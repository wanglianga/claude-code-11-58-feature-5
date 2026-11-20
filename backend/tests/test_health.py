"""设备健康六类验收场景的端到端验证:
季节负荷变化不报警、慢性劣化持续报警、突发故障报警、部件更换重新起算、
校准失效/条件缺失取消报警资格(不默认正常)、维修记录迟到重标健康期且旧报警保留依据。
"""


def _trend_runs(client, auth, install_id=None):
    params = {"component_install_id": install_id} if install_id else {}
    r = client.get("/api/health/trend-runs", params=params, headers=auth)
    assert r.status_code == 200
    return r.json()


def _trend_detail(client, auth, tr_id):
    r = client.get(f"/api/health/trend-runs/{tr_id}", headers=auth)
    assert r.status_code == 200
    return r.json()


def _alerts(client, auth):
    r = client.get("/api/health/alerts", headers=auth)
    assert r.status_code == 200
    return r.json()


def _installs(client, auth):
    r = client.get("/api/health/components", headers=auth)
    assert r.status_code == 200
    return r.json()


def _point(points, run_key):
    return next(p for p in points if p["run_key"] == run_key)


def test_health_profile_complete(client, auth):
    """健康档案:安装/更换区间、维修与故障时间、校准状态、允许关注的层级。"""
    r = client.get("/api/health/profile", headers=auth)
    assert r.status_code == 200
    p = r.json()
    # 允许关注的设备层级:部件级牵引电机启用,整车级未启用
    wc = {(c["level"], c["component_type"]): c["enabled"] for c in p["watch_configs"]}
    assert wc[("COMPONENT", "TRACTION_MOTOR")] is True
    assert wc[("TRAIN", None)] is False
    # 部件安装/更换区间:T02/T03 各有一次更换(退役+在役)
    installs = p["installs"]
    assert len(installs) == 5
    t02 = [i for i in installs if i["train"] == "T02"]
    assert len(t02) == 2 and any(i["removed_at"] for i in t02) \
        and any(i["active"] for i in t02)
    # 维修与故障时间:含发生/登记两个时刻;T01 故障为晚到记录
    t01_faults = [e for e in p["maintenance"]
                  if e["train_id"] == next(i["train_id"] for i in installs
                                           if i["train"] == "T01")
                  and e["type"] == "FAULT"]
    assert len(t01_faults) == 1 and t01_faults[0]["late"] is True
    assert t01_faults[0]["occurred_at"] < t01_faults[0]["recorded_at"]
    # 校准状态:OB-T02 经历 VALID->EXPIRED->FAILED->VALID
    cal = [c for c in p["calibrations"] if c["serial"] == "OB-T02"]
    assert {c["status"] for c in cal} >= {"VALID", "EXPIRED", "FAILED"}


def test_seasonal_load_conditioned_not_alerted(client, auth):
    """季节负荷变化:夏季高温运行绝对能耗高,但落在高温条件层基线内,不报警。"""
    installs = _installs(client, auth)
    t01 = next(i for i in installs if i["train"] == "T01")
    tr = _trend_runs(client, auth, t01["id"])[-1]  # 最早一批(SEED)
    detail = _trend_detail(client, auth, tr["id"])
    hot = _point(detail["points"], "RUN-2005")   # 33°C 高温运行
    mild = _point(detail["points"], "RUN-2009")  # 20°C 常态运行
    assert hot["actual_per_km"] > mild["baseline_hi"]  # 绝对能耗显著高于常态基线
    assert hot["baseline_mid"] > mild["baseline_mid"]  # 高温层基线本身更高
    assert hot["baseline_level"] == 1                  # 用到最细条件层(含温度带)
    assert hot["excess"] is False and hot["eligible"] == "ELIGIBLE"
    # 全部高温运行都不超限
    for key in ("RUN-2001", "RUN-2002", "RUN-2003", "RUN-2004", "RUN-2005"):
        assert _point(detail["points"], key)["excess"] is False
    # T01 的报警只来自 11 月劣化链,不含 8 月高温运行
    for a in _alerts(client, auth):
        assert not a["first_excess_at"].startswith("2026-08")


def test_chronic_degradation_alert(client, auth):
    """慢性劣化:条件调整后残差持续超限 -> DEGRADATION 报警,含持续长度与超额能耗。"""
    installs = _installs(client, auth)
    t01 = next(i for i in installs if i["train"] == "T01")
    alerts = [a for a in _alerts(client, auth)
              if a["component_install_id"] == t01["id"]]
    assert len(alerts) == 1
    a = alerts[0]
    assert a["type"] == "DEGRADATION" and a["eligibility"] == "QUALIFIED"
    assert a["persist_runs"] >= 3                 # 持续长度
    assert a["max_residual_pct"] > 0.3            # 条件调整后仍显著偏离
    assert a["excess_kwh"] > 0                    # 超额能耗残差
    detail = client.get(f"/api/health/alerts/{a['id']}", headers=auth).json()
    chain_keys = [p["run_key"] for p in detail["points"]]
    assert chain_keys == ["RUN-2017", "RUN-2018", "RUN-2019", "RUN-2020", "RUN-2021"]
    # 报警与原始能耗、条件变量关联
    for p in detail["points"]:
        assert p["energy"]["net_kwh"] > 0 and p["energy"]["quality"] == "GOOD"
        assert p["condition"]["temp_band"] == "MILD"
        assert p["condition"]["load_band"] == "MID"
    # 当时依据快照:产生报警时无已知故障
    assert detail["basis"]["health_note"].startswith("无已知故障")
    assert detail["basis"]["known_events"] == []


def test_sudden_failure_alert(client, auth):
    """突发故障:单点残差剧增 -> SUDDEN 报警,当日故障记录回查确认。"""
    installs = _installs(client, auth)
    t03a = next(i for i in installs if i["train"] == "T03" and not i["active"])
    alerts = [a for a in _alerts(client, auth)
              if a["component_install_id"] == t03a["id"]]
    assert len(alerts) == 1
    a = alerts[0]
    assert a["type"] == "SUDDEN" and a["eligibility"] == "QUALIFIED"
    assert a["persist_runs"] == 1 and a["max_residual_pct"] > 0.35
    detail = client.get(f"/api/health/alerts/{a['id']}", headers=auth).json()
    assert [p["run_key"] for p in detail["points"]] == ["RUN-2041"]
    # 维修回查:故障记录(当天登记,非晚到)确认了该报警
    confirmed = [e for e in detail["linked_maintenance"] if e["confirms"]]
    assert any(e["type"] == "FAULT" and not e["late"] for e in confirmed)
    assert detail["confirmed_by"]


def test_component_replacement_restarts_trend(client, auth):
    """部件更换:新旧安装周期各自独立趋势运行,新周期从更换后重新起算。"""
    installs = _installs(client, auth)
    t02 = sorted([i for i in installs if i["train"] == "T02"],
                 key=lambda i: i["installed_at"])
    old, new = t02
    tr_old = _trend_runs(client, auth, old["id"])[-1]
    tr_new = _trend_runs(client, auth, new["id"])[-1]
    d_old = _trend_detail(client, auth, tr_old["id"])
    d_new = _trend_detail(client, auth, tr_new["id"])
    # 旧周期:6 个健康点,无报警;新周期:从更换后第一个运行开始
    assert d_old["summary"]["n_points"] == 6
    starts = [p["started_at"] for p in d_new["points"]]
    assert min(starts) >= new["installed_at"]
    assert all(p["started_at"] >= new["installed_at"] for p in d_new["points"])
    # 新部件基线来自车队同条件健康样本(少样本可解释),层级为 1
    p0 = d_new["points"][0]
    assert p0["baseline_level"] == 1 and p0["baseline_samples"] >= 4


def test_calibration_and_missing_condition_cancel_eligibility(client, auth):
    """校准失效/条件缺失/能耗估计:取消或降低报警资格,不默认为正常。"""
    installs = _installs(client, auth)
    t02b = next(i for i in installs if i["train"] == "T02" and i["active"])
    tr = _trend_runs(client, auth, t02b["id"])[-1]
    detail = _trend_detail(client, auth, tr["id"])
    pts = detail["points"]
    # 校准过期 -> 降级(可支撑报警但报警降级)
    p_exp = _point(pts, "RUN-2028")
    assert p_exp["eligible"] == "DOWNGRADED"
    assert "CALIBRATION_EXPIRED" in p_exp["eligibility_reasons"]
    # 校准失效 -> 取消资格;期间高能耗不报警也不判正常
    p_fail = _point(pts, "RUN-2031")
    assert p_fail["eligible"] == "INELIGIBLE"
    assert "CALIBRATION_FAILED" in p_fail["eligibility_reasons"]
    assert p_fail["excess"] is True                 # 高能耗事实保留
    # 条件缺失 -> 取消资格,无基线
    p_miss = _point(pts, "RUN-2032")
    assert p_miss["eligible"] == "INELIGIBLE"
    assert "CONDITION_MISSING" in p_miss["eligibility_reasons"]
    assert p_miss["baseline_mid"] is None
    # 能耗为估计值(设备重启)-> 取消资格
    p_est = _point(pts, "RUN-1002")
    assert p_est["eligible"] == "INELIGIBLE"
    assert "ENERGY_ESTIMATED" in p_est["eligibility_reasons"]
    # 上述运行都没有被默认为正常,也没有产生报警
    for key in ("RUN-2028", "RUN-2031", "RUN-2032", "RUN-1002"):
        assert _point(pts, key)["eligible"] != "ELIGIBLE"
    assert _alerts(client, auth) and not any(
        a["component_install_id"] == t02b["id"] for a in _alerts(client, auth))
    # 覆盖不足区间与资格取消的高能耗被显式汇总
    s = detail["summary"]
    assert s["n_ineligible"] >= 3 and s["suppressed_excess"] >= 1
    assert len(s["coverage_gaps"]) >= 2


def test_downgraded_alert_when_calibration_expired(client, auth):
    """校准过期期间持续超限:报警资格降级(DOWNGRADED),不取消也不默认正常。"""
    installs = _installs(client, auth)
    t03b = next(i for i in installs if i["train"] == "T03" and i["active"])
    alerts = [a for a in _alerts(client, auth)
              if a["component_install_id"] == t03b["id"]]
    assert len(alerts) == 1
    a = alerts[0]
    assert a["type"] == "DEGRADATION" and a["eligibility"] == "DOWNGRADED"
    assert a["persist_runs"] >= 3
    detail = client.get(f"/api/health/alerts/{a['id']}", headers=auth).json()
    assert all("CALIBRATION_EXPIRED" in p["eligibility_reasons"]
               for p in detail["points"])
    # 该报警针对更换后的新部件,旧部件的故障记录不被错误关联
    assert detail["confirmed_by"] == []


def test_late_maintenance_relabels_health_keeps_old_basis(client, auth):
    """维修记录迟到:触发新趋势运行并重标健康期;旧报警保留当时依据。"""
    installs = _installs(client, auth)
    t01 = next(i for i in installs if i["train"] == "T01")
    trs = _trend_runs(client, auth, t01["id"])
    assert {t["trigger"] for t in trs} >= {"SEED", "LATE_MAINTENANCE"}
    late = next(t for t in trs if t["trigger"] == "LATE_MAINTENANCE")
    detail = _trend_detail(client, auth, late["id"])
    pts = detail["points"]
    # 健康期重标:故障前窗口排除出基线,故障后运行不再参与
    assert _point(pts, "RUN-2018")["health_state"] == "EXCLUDED_PRE_FAULT"
    assert _point(pts, "RUN-2020")["health_state"] == "EXCLUDED_PRE_FAULT"
    p_post = _point(pts, "RUN-2021")
    assert p_post["health_state"] == "POST_FAULT"
    assert p_post["eligible"] == "INELIGIBLE"
    assert "POST_FAULT" in p_post["eligibility_reasons"]
    assert detail["summary"]["health_note"].startswith("已知故障")
    # 旧报警保留当时依据(产生时无已知故障),不被晚到记录改写
    alerts = [a for a in _alerts(client, auth)
              if a["component_install_id"] == t01["id"]]
    assert len(alerts) == 1  # 结论一致,不重复报警
    basis = client.get(f"/api/health/alerts/{alerts[0]['id']}",
                       headers=auth).json()["basis"]
    assert basis["trigger"] == "SEED" and basis["known_events"] == []
    # 维修回查:晚到的故障记录确认了该报警,且标记为晚到
    detail_a = client.get(f"/api/health/alerts/{alerts[0]['id']}",
                          headers=auth).json()
    confirmed = [e for e in detail_a["linked_maintenance"] if e["confirms"]]
    assert any(e["type"] == "FAULT" and e["late"] for e in confirmed)


def test_post_late_maintenance_triggers_reanalysis(client, auth):
    """登记晚到维修记录 -> 自动触发新一轮趋势运行;准时登记不触发。"""
    installs = _installs(client, auth)
    t02b = next(i for i in installs if i["train"] == "T02" and i["active"])
    before = _trend_runs(client, auth)
    # 晚到登记(发生 11-03,现在才登记)
    r = client.post("/api/health/maintenance", headers=auth, json={
        "train_id": t02b["train_id"], "type": "OVERHAUL",
        "occurred_at": "2026-11-03T10:00:00",
        "description": "晚到登记的例行检修(测试)"})
    assert r.status_code == 200
    body = r.json()
    assert body["late"] is True
    assert body["triggered_trend_run_ids"]
    after = _trend_runs(client, auth)
    assert len(after) == len(before) + len(body["triggered_trend_run_ids"])
    # 准时登记(发生即登记)-> 不触发
    r2 = client.post("/api/health/maintenance", headers=auth, json={
        "train_id": t02b["train_id"], "type": "OVERHAUL",
        "occurred_at": "2026-11-26T10:00:00",
        "recorded_at": "2026-11-26T10:30:00",
        "description": "准时登记(测试)"})
    assert r2.json()["late"] is False
    assert r2.json()["triggered_trend_run_ids"] == []


def test_manual_trend_run_and_condition_upsert(client, auth):
    """手动触发趋势运行;补登运行条件后重新分析可使资格恢复。"""
    installs = _installs(client, auth)
    t02b = next(i for i in installs if i["train"] == "T02" and i["active"])
    # 补登 RUN-2032 的运行条件
    r = client.post("/api/health/conditions", headers=auth,
                    json={"run_key": "RUN-2032", "ambient_temp_c": 15,
                          "weather": "CLEAR", "speed_restriction": False})
    assert r.status_code == 200
    # 手动触发该安装周期的新趋势运行
    r = client.post("/api/health/trend-runs", headers=auth,
                    json={"component_install_id": t02b["id"]})
    assert r.status_code == 200
    tr_id = r.json()["trend_run_ids"][0]
    detail = _trend_detail(client, auth, tr_id)
    p = _point(detail["points"], "RUN-2032")
    assert "CONDITION_MISSING" not in p["eligibility_reasons"]
    assert p["baseline_mid"] is not None  # 条件补齐后获得基线
