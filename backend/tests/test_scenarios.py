"""五类验收场景的端到端验证:能量方向、可比范围、单位指标、历史复盘。"""


def _runs(client, auth):
    r = client.get("/api/runs?all_versions=true", headers=auth)
    assert r.status_code == 200
    return r.json()


def _run_by_key(runs, key, version=None):
    cands = [r for r in runs if r["run_key"] == key]
    if version:
        cands = [r for r in cands if r["version"] == version]
    return max(cands, key=lambda r: r["version"])


def _recon(client, auth, run_id, method):
    r = client.get(f"/api/runs/{run_id}/reconciliation?method={method}", headers=auth)
    assert r.status_code == 200
    return r.json()


def test_login_required(client):
    assert client.get("/api/runs").status_code == 401


def test_turnback_segments_and_direction(client, auth):
    """跨区段折返:片段在折返点切断,方向翻转,双侧可闭合。"""
    runs = _runs(client, auth)
    r1 = _run_by_key(runs, "RUN-1001")
    detail = client.get(f"/api/runs/{r1['id']}", headers=auth).json()
    reasons = [s["cut_reason"] for s in detail["segments"]]
    dirs = [s["direction"] for s in detail["segments"]]
    assert "TURNBACK" in reasons and "STOP" in reasons
    assert "UP" in dirs and "DOWN" in dirs  # 折返后方向翻转
    # 每个片段都有车载+供电两侧能量,方向量齐全
    for seg in detail["segments"]:
        srcs = {f["source"] for f in seg["fragments"]}
        assert srcs == {"ONBOARD", "SUPPLY"}
        onb = next(f for f in seg["fragments"] if f["source"] == "ONBOARD")
        assert set(onb["quantities"]) == {"traction", "regen", "aux"}
    rec = _recon(client, auth, r1["id"], "RUN_ALIGNED")
    assert rec["comparable"] == "FULL"
    assert rec["diff"]["v"] > 0  # 供电净受电 > 车载净能耗(线损)
    att = rec["attribution"]
    total = att["line_loss"] + att["regen_not_returned"] + att["metering_delay"] + att["unknown"]
    assert abs(total - rec["diff"]["v"]) < 0.01  # 归因之和=差异


def test_device_reset_not_subtracted(client, auth):
    """设备重启:切断片段,累计值倒退不直接相减。"""
    runs = _runs(client, auth)
    r2 = _run_by_key(runs, "RUN-1002")
    detail = client.get(f"/api/runs/{r2['id']}", headers=auth).json()
    reasons = [s["cut_reason"] for s in detail["segments"]]
    assert "DEVICE_RESET" in reasons
    assert "CONSIST_CHANGE" in reasons       # 换编组切断
    assert "MULTIPLIER_CHANGE" in reasons    # 倍率区间切换切断
    reset_seg = next(s for s in detail["segments"] if s["cut_reason"] == "DEVICE_RESET")
    onb = next(f for f in reset_seg["fragments"] if f["source"] == "ONBOARD")
    assert onb["quality"] == "ESTIMATED"
    assert onb["detail"]["resets"]           # 重启事件被记录
    # 重启段牵引 = 重启前单调段,不含负跳变;上界 > 下界
    assert onb["quantities"]["traction"]["v"] == 2.5
    assert onb["quantities"]["traction"]["hi"] > onb["quantities"]["traction"]["lo"]
    rec = _recon(client, auth, r2["id"], "RUN_ALIGNED")
    assert rec["comparable"] == "PARTIAL"
    assert any(u["code"] == "DEVICE_RESET" for u in rec["undetermined"])


def test_regen_not_returned_attribution(client, auth):
    """回馈受限:电网不接受回馈时,差异归因到'回馈未上网'。"""
    runs = _runs(client, auth)
    r3 = _run_by_key(runs, "RUN-1003")
    rec = _recon(client, auth, r3["id"], "RUN_ALIGNED")
    assert rec["comparable"] == "FULL"
    assert rec["attribution"]["regen_not_returned"] > 0
    assert rec["attribution"]["assumptions"]["regen_accepted"] is False
    # 车载回馈显著但供电返送为 0
    detail = client.get(f"/api/runs/{r3['id']}", headers=auth).json()
    seg = detail["segments"][0]
    onb = next(f for f in seg["fragments"] if f["source"] == "ONBOARD")
    sup = next(f for f in seg["fragments"] if f["source"] == "SUPPLY")
    assert onb["quantities"]["regen"]["v"] == 10.0
    assert sup["quantities"]["export"]["v"] == 0.0


def test_onboard_supply_difference(client, auth):
    """车载与供电表差异:差异被分解为线损/回馈未上网/不可知。"""
    runs = _runs(client, auth)
    r4 = _run_by_key(runs, "RUN-1004")
    rec = _recon(client, auth, r4["id"], "RUN_ALIGNED")
    assert rec["diff"]["v"] > 5  # 显著差异
    att = rec["attribution"]
    assert att["line_loss"] > 0 and att["unknown"] > 0
    total = att["line_loss"] + att["regen_not_returned"] + att["metering_delay"] + att["unknown"]
    assert abs(total - rec["diff"]["v"]) < 0.01


def test_late_readings_new_version_keeps_published(client, auth):
    """补传读数:只生成新运行版本,已发布指标保留原水位与差异说明。"""
    runs = _runs(client, auth)
    v1 = _run_by_key(runs, "RUN-1005", version=1)
    v2 = _run_by_key(runs, "RUN-1005", version=2)
    assert v2["supersedes_id"] == v1["id"]
    # v1 缺测:车载净能耗未知(不冒充闭合);v2 补传后闭合
    rec1 = _recon(client, auth, v1["id"], "RUN_ALIGNED")
    rec2 = _recon(client, auth, v2["id"], "RUN_ALIGNED")
    assert rec1["onboard_net"]["v"] is None
    assert rec1["comparable"] == "PARTIAL"
    assert rec2["onboard_net"]["v"] == 18.0
    assert rec2["comparable"] == "FULL"
    # 指标:v1 保留原水位(未知),v2 带差异说明并 supersede
    metrics = client.get("/api/metrics?scope=RUN&scope_key=RUN-1005", headers=auth).json()
    net = sorted([m for m in metrics if m["name"] == "NET_KWH"], key=lambda m: m["id"])
    assert len(net) == 2
    assert net[0]["value"] is None and net[0]["run_version"] == 1
    assert net[1]["value"] == 18.0 and net[1]["run_version"] == 2
    assert net[1]["supersedes_id"] == net[0]["id"]
    assert "迟到补传" in net[1]["diff_note"]


def test_supply_missing_outputs_unknown(client, auth):
    """供电表缺失:输出未知范围,不以车载值冒充闭合计量。"""
    runs = _runs(client, auth)
    r6 = _run_by_key(runs, "RUN-1006")
    rec = _recon(client, auth, r6["id"], "RUN_ALIGNED")
    assert rec["comparable"] == "NONE"
    assert rec["supply_net"]["v"] is None
    assert rec["diff"]["v"] is None
    assert any(u["code"] == "SUPPLY_COVERAGE_INSUFFICIENT" for u in rec["undetermined"])
    # 车载侧仍有值,但差异不可判定
    assert rec["onboard_net"]["v"] == 11.25


def test_both_methods_same_run_range(client, auth):
    """两条路径对同一运行范围都给出可比较能耗与差异边界。"""
    runs = _runs(client, auth)
    r1 = _run_by_key(runs, "RUN-1001")
    ra = _recon(client, auth, r1["id"], "RUN_ALIGNED")
    ga = _recon(client, auth, r1["id"], "GRID_ALIGNED")
    assert ra["onboard_net"]["v"] == ga["onboard_net"]["v"] == 36.0
    assert ra["supply_net"]["v"] == ga["supply_net"]["v"] == 40.0
    assert ra["diff"]["v"] == ga["diff"]["v"] == 4.0
    assert any(u["code"] == "GRID_EDGE_APPORTIONED" for u in ga["undetermined"])


def test_metrics_scopes_and_replay(client, auth):
    """指标按运行/区段/班次/车型发布;复盘可回查构成。"""
    metrics = client.get("/api/metrics", headers=auth).json()
    scopes = {m["scope"] for m in metrics}
    assert {"RUN", "SECTION", "SHIFT", "VEHICLE_MODEL"} <= scopes
    names = {m["name"] for m in metrics}
    assert {"NET_KWH", "KWH_PER_KM", "KWH_PER_PASSENGER", "REGEN_RATIO"} <= names
    # 复盘:指标 -> 采样片段/配置版本/核对结论
    m = next(x for x in metrics if x["scope"] == "RUN" and x["scope_key"] == "RUN-1001"
             and x["name"] == "NET_KWH")
    replay = client.get(f"/api/metrics/{m['id']}/replay", headers=auth).json()
    assert replay["metric"]["value"] == 36.0
    assert replay["runs"] and replay["runs"][0]["segments"]
    assert replay["reading_ids"]                      # 采样片段可回查
    assert replay["reconciliations"]                  # 核对结论可回查
    kinds = {c["kind"] for c in replay["config_versions"]}
    assert "MULTIPLIER" in kinds and "REGEN_ACCEPT" in kinds
    # 迟到补传的指标:复盘可见历史水位
    m2 = next(x for x in metrics if x["scope_key"] == "RUN-1005"
              and x["name"] == "NET_KWH" and x["run_version"] == 2)
    replay2 = client.get(f"/api/metrics/{m2['id']}/replay", headers=auth).json()
    assert replay2["history"] and replay2["history"][0]["value"] is None


def test_ingest_late_reading_triggers_new_version(client, auth):
    """通过采集接口补传迟到读数,自动生成新运行版本。"""
    runs = _runs(client, auth)
    r4 = _run_by_key(runs, "RUN-1004")
    before = client.get("/api/metrics?scope=RUN&scope_key=RUN-1004", headers=auth).json()
    payload = [{
        "device_serial": "OB-T01",
        "sampled_at": "2026-10-21T14:05:00",
        "received_at": "2026-10-21T16:05:00",  # 迟到 2 小时
        "seq": 90001, "traction_cum": 0, "regen_cum": 0, "aux_cum": 0,
        "multiplier": 1.0, "comm_status": "OK",
    }]
    r = client.post("/api/readings", json=payload, headers=auth)
    assert r.status_code == 200
    assert r.json()["late_new_versions"] == [{"run_key": "RUN-1004", "version": 2}]
    after = client.get("/api/metrics?scope=RUN&scope_key=RUN-1004", headers=auth).json()
    assert len(after) > len(before)  # 新版本指标已发布,旧指标保留
