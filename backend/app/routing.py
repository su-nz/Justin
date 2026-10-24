"""動態派車（Dynamic Dispatch）。

做法：
1. 選桶：已滿 / 預測在 horizon 內會滿 / 有未處理的 overflow、illegal_dumping 事件 → 必收；
   將滿（≥ NEAR_FULL）→ 選收（可被丟棄，但有懲罰值，順路就收）。
2. 求解：容量限制車輛路徑問題（CVRP），OR-Tools；沒裝 OR-Tools 時退回最近鄰 + 2-opt。
3. 對照組：固定排程（每桶都跑一次）的距離，算出節省比例，方便和 Paper 1（-30.76%）比較。

距離用直線距離 × ROAD_FACTOR 近似道路距離；正式版請換成 OSRM / Google Distance Matrix 的距離矩陣。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .logic import FULL_PCT, NEAR_FULL_PCT, haversine_m

ROAD_FACTOR = 1.35

try:
    from ortools.constraint_solver import pywrapcp, routing_enums_pb2
    HAVE_ORTOOLS = True
except ImportError:  # pragma: no cover
    HAVE_ORTOOLS = False


@dataclass
class Stop:
    bin_id: str
    name: str
    lat: float
    lng: float
    demand_l: int
    mandatory: bool
    reason: str


@dataclass
class Plan:
    routes: list[dict] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    total_m: float = 0.0
    baseline_m: float = 0.0
    saving_pct: float = 0.0
    solver: str = ""


def select_stops(bins: list[dict], horizon_h: float) -> list[Stop]:
    stops = []
    for b in bins:
        if b["state"] == "offline" and not b.get("open_events"):
            continue
        fill = b["fill_pct"] or 0.0
        projected = fill + (b.get("rate_pct_per_h") or 0.0) * horizon_h
        reason = None
        mandatory = False
        if b.get("open_events"):
            mandatory, reason = True, "事件：" + "、".join(sorted(set(b["open_events"])))
        elif fill >= FULL_PCT:
            mandatory, reason = True, f"已滿 {fill:.0f}%"
        elif projected >= FULL_PCT:
            mandatory, reason = True, f"預測 {horizon_h:g}h 內滿（{projected:.0f}%）"
        elif fill >= NEAR_FULL_PCT:
            reason = f"將滿 {fill:.0f}%（順路收）"
        elif b["state"] == "fault":
            reason = "感測器異常，順路檢查"
        if reason:
            demand = max(1, round(b["capacity_l"] * min(max(projected, fill), 100) / 100))
            stops.append(Stop(b["id"], b["name"], b["lat"], b["lng"], demand, mandatory, reason))
    return stops


def _dist_matrix(points: list[tuple[float, float]]) -> list[list[int]]:
    n = len(points)
    return [[0 if i == j else int(haversine_m(*points[i], *points[j]) * ROAD_FACTOR)
             for j in range(n)] for i in range(n)]


def _tour_len(order: list[int], d) -> float:
    return sum(d[order[k]][order[k + 1]] for k in range(len(order) - 1))


def _nn_2opt(nodes: list[int], d) -> list[int]:
    """單車 TSP 近似解：最近鄰 + 2-opt。用於 baseline 與無 OR-Tools 的退路。"""
    if not nodes:
        return [0, 0]
    rest, tour = set(nodes), [0]
    while rest:
        nxt = min(rest, key=lambda j: d[tour[-1]][j])
        tour.append(nxt)
        rest.remove(nxt)
    tour.append(0)
    improved = True
    while improved:
        improved = False
        for i in range(1, len(tour) - 2):
            for k in range(i + 1, len(tour) - 1):
                delta = (d[tour[i - 1]][tour[k]] + d[tour[i]][tour[k + 1]]
                         - d[tour[i - 1]][tour[i]] - d[tour[k]][tour[k + 1]])
                if delta < -1:
                    tour[i:k + 1] = reversed(tour[i:k + 1])
                    improved = True
    return tour


def plan_routes(depot: tuple[float, float], bins: list[dict], vehicles: int = 1,
                vehicle_capacity_l: int = 2000, horizon_h: float = 4.0,
                time_limit_s: int = 5) -> Plan:
    stops = select_stops(bins, horizon_h)
    plan = Plan()

    # 對照組：固定排程 → 所有在線的桶都繞一圈
    all_pts = [depot] + [(b["lat"], b["lng"]) for b in bins]
    d_all = _dist_matrix(all_pts)
    plan.baseline_m = _tour_len(_nn_2opt(list(range(1, len(all_pts))), d_all), d_all)

    if not stops:
        plan.solver = "none"
        return plan

    pts = [depot] + [(s.lat, s.lng) for s in stops]
    d = _dist_matrix(pts)
    demands = [0] + [s.demand_l for s in stops]

    if HAVE_ORTOOLS:
        plan.solver = "ortools-cvrp"
        mgr = pywrapcp.RoutingIndexManager(len(pts), vehicles, 0)
        rt = pywrapcp.RoutingModel(mgr)
        cb = rt.RegisterTransitCallback(lambda i, j: d[mgr.IndexToNode(i)][mgr.IndexToNode(j)])
        rt.SetArcCostEvaluatorOfAllVehicles(cb)
        dcb = rt.RegisterUnaryTransitCallback(lambda i: demands[mgr.IndexToNode(i)])
        rt.AddDimensionWithVehicleCapacity(dcb, 0, [vehicle_capacity_l] * vehicles, True, "Cap")
        for idx, s in enumerate(stops, start=1):
            # 必收：極大懲罰（實質上不可丟）；選收：懲罰 = 繞路 1.5 km 的成本
            rt.AddDisjunction([mgr.NodeToIndex(idx)], 10_000_000 if s.mandatory else 1500)
        p = pywrapcp.DefaultRoutingSearchParameters()
        p.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
        p.local_search_metaheuristic = routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
        p.time_limit.seconds = time_limit_s
        sol = rt.SolveWithParameters(p)
        if sol is None:
            raise RuntimeError("無可行解：請增加車輛數或容量")
        visited = set()
        for v in range(vehicles):
            i = rt.Start(v)
            order = []
            while not rt.IsEnd(i):
                order.append(mgr.IndexToNode(i))
                i = sol.Value(rt.NextVar(i))
            order.append(0)
            if len(order) > 2:
                visited.update(order[1:-1])
                plan.routes.append(_route_dict(v, order, pts, stops, demands, d))
        plan.skipped = [stops[k - 1].bin_id for k in range(1, len(pts)) if k not in visited]
    else:  # 退路：單車
        plan.solver = "nn-2opt"
        order = _nn_2opt(list(range(1, len(pts))), d)
        plan.routes.append(_route_dict(0, order, pts, stops, demands, d))

    plan.total_m = sum(r["distance_m"] for r in plan.routes)
    if plan.baseline_m:
        plan.saving_pct = round(100 * (1 - plan.total_m / plan.baseline_m), 1)
    return plan


def _route_dict(v, order, pts, stops, demands, d) -> dict:
    return {
        "vehicle": v,
        "distance_m": _tour_len(order, d),
        "load_l": sum(demands[k] for k in order),
        "path": [{"lat": pts[k][0], "lng": pts[k][1],
                  **({"bin_id": stops[k - 1].bin_id, "name": stops[k - 1].name,
                      "reason": stops[k - 1].reason, "mandatory": stops[k - 1].mandatory}
                     if k else {"depot": True})} for k in order],
    }
