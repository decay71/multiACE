"""Print queue: staged files and the loadout check at launch."""
import pytest

from multiace.tools import job_history as jh
from .test_history_api import live  # noqa: F401


def _slot(ace, slot, material="PLA", color="ff0000"):
    return {"ace": ace, "slot": slot, "material": material, "color": color}


class TestLoadoutChanges:
    def test_same_spools_report_nothing(self):
        exp = [_slot(0, 1), _slot(1, 2, "PETG", "00ff00")]
        live_ = [_slot(0, 1), _slot(1, 2, "PETG", "00ff00"), _slot(0, 3)]
        assert jh.loadout_changes(exp, live_) == []

    def test_a_different_colour_is_a_change(self):
        out = jh.loadout_changes([_slot(0, 1)], [_slot(0, 1, color="0000ff")])
        assert len(out) == 1
        assert out[0]["expected"]["color"] == "ff0000"
        assert out[0]["now"]["color"] == "0000ff"

    def test_a_different_material_is_a_change(self):
        out = jh.loadout_changes([_slot(0, 1)], [_slot(0, 1, "PETG")])
        assert [(c["ace"], c["slot"]) for c in out] == [(0, 1)]

    def test_an_emptied_slot_is_a_change(self):
        out = jh.loadout_changes([_slot(0, 1)], [])
        assert out[0]["now"] is None

    def test_case_and_hash_prefix_do_not_matter(self):
        out = jh.loadout_changes([_slot(0, 1, "pla", "#FF0000")],
                                 [_slot(0, 1, "PLA", "ff0000")])
        assert out == []

    def test_slots_the_file_does_not_use_are_ignored(self):
        assert jh.loadout_changes([_slot(0, 1)],
                                  [_slot(0, 1), _slot(0, 2, "ABS", "000000")]
                                  ) == []


class TestQueueStore:
    def test_round_trip_and_order(self, tmp_path):
        jh.add_queue_entry(str(tmp_path), {"id": "a", "filename": "a"})
        jh.add_queue_entry(str(tmp_path), {"id": "b", "filename": "b"})
        assert [e["id"] for e in jh.load_queue(str(tmp_path))] == ["a", "b"]

    def test_corrupt_file_is_an_empty_queue(self, tmp_path):
        (tmp_path / "queue.json").write_text("{nope", encoding="utf-8")
        assert jh.load_queue(str(tmp_path)) == []

    def test_bounded(self, tmp_path, monkeypatch):
        monkeypatch.setattr(jh, "MAX_QUEUE", 2)
        assert jh.add_queue_entry(str(tmp_path), {"id": "a"})
        assert jh.add_queue_entry(str(tmp_path), {"id": "b"})
        assert jh.add_queue_entry(str(tmp_path), {"id": "c"}) is False

    def test_remove_returns_the_entry(self, tmp_path):
        jh.add_queue_entry(str(tmp_path), {"id": "a", "path": "p"})
        assert jh.remove_queue_entry(str(tmp_path), "a")["path"] == "p"
        assert jh.remove_queue_entry(str(tmp_path), "a") is None


@pytest.fixture
def q(live, monkeypatch):  # noqa: F811
    main, c, data = live
    state = {"slots": [_slot(0, 0), _slot(0, 1, "PETG", "00ff00"),
                       _slot(1, 0, "PLA", "0000ff")],
             "started": [], "deleted": [], "print_state": "standby"}

    async def fake_live():
        return list(state["slots"])

    async def fake_get(path):
        return {"result": {"status": {"print_stats":
                                      {"state": state["print_state"]}}}}

    async def fake_post(path, body=None, timeout=30.0):
        state["started"].append((path, body))
        return {}

    class _Client:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def delete(self, url):
            state["deleted"].append(url)

    monkeypatch.setattr(main, "_live_slots_async", fake_live)
    monkeypatch.setattr(main, "_mr_get", fake_get)
    monkeypatch.setattr(main, "_mr_post", fake_post)
    monkeypatch.setattr(main.httpx, "AsyncClient", _Client)
    return main, c, state


def _add(c, used_slots=((0, 0), (1, 1)), name="a.gcode",
         path="multiace_queue/x_a.gcode", mode="slicer"):
    return c.post("/api/queue", json={
        "filename": name, "path": path, "mode": mode,
        "used_slots": [list(p) for p in used_slots]})


class TestQueueApi:
    def test_add_snapshots_only_the_used_slots(self, q):
        _, c, _ = q
        r = _add(c)  # ace0/slot0 is loaded, ace1/slot1 is empty -> dropped
        assert r.status_code == 200
        slots = r.json()["entry"]["slots"]
        assert [(s["ace"], s["slot"]) for s in slots] == [(0, 0)]

    def test_head_mode_uses_the_slots_of_its_swap_lines_too(self, q):
        _, c, _ = q
        r = _add(c, used_slots=[(0, 1)], name="h.gcode", mode="head",
                 path="multiace_queue/x_h.gcode")
        assert [(s["ace"], s["slot"]) for s in r.json()["entry"]["slots"]
                ] == [(0, 1)]

    def test_no_slot_list_snapshots_every_identified_slot(self, q):
        _, c, _ = q
        r = c.post("/api/queue", json={"filename": "h.gcode", "mode": "head",
                                       "path": "multiace_queue/x_h.gcode"})
        assert len(r.json()["entry"]["slots"]) == 3

    def test_list_reports_no_changes_while_the_loadout_matches(self, q):
        _, c, _ = q
        _add(c)
        entry = c.get("/api/queue").json()["queue"][0]
        assert entry["changes"] == []

    def test_list_reports_a_swapped_spool(self, q):
        _, c, state = q
        _add(c)
        state["slots"][0] = _slot(0, 0, "PLA", "ffffff")
        entry = c.get("/api/queue").json()["queue"][0]
        assert [(x["ace"], x["slot"]) for x in entry["changes"]] == [(0, 0)]

    def test_launch_starts_the_file_and_leaves_the_queue(self, q):
        _, c, state = q
        _add(c)
        qid = c.get("/api/queue").json()["queue"][0]["id"]
        assert c.post(f"/api/queue/{qid}/launch").status_code == 200
        assert state["started"] == [("/printer/print/start",
                                     {"filename": "multiace_queue/x_a.gcode"})]
        assert c.get("/api/queue").json()["queue"] == []

    def test_launch_is_refused_when_a_spool_changed(self, q):
        _, c, state = q
        _add(c)
        qid = c.get("/api/queue").json()["queue"][0]["id"]
        state["slots"][0] = _slot(0, 0, "PETG", "ff0000")
        r = c.post(f"/api/queue/{qid}/launch")
        assert r.status_code == 409
        assert r.json()["detail"]["changes"][0]["slot"] == 0
        assert state["started"] == []
        assert len(c.get("/api/queue").json()["queue"]) == 1

    def test_launch_is_refused_while_printing(self, q):
        _, c, state = q
        _add(c)
        qid = c.get("/api/queue").json()["queue"][0]["id"]
        state["print_state"] = "printing"
        assert c.post(f"/api/queue/{qid}/launch").status_code == 409
        assert state["started"] == []

    def test_unknown_entry_is_404(self, q):
        _, c, _ = q
        assert c.post("/api/queue/nope/launch").status_code == 404
        assert c.delete("/api/queue/nope").status_code == 404

    def test_remove_deletes_the_staged_file(self, q):
        _, c, state = q
        _add(c)
        qid = c.get("/api/queue").json()["queue"][0]["id"]
        assert c.delete(f"/api/queue/{qid}").status_code == 200
        assert state["deleted"][0].endswith(
            "/server/files/gcodes/multiace_queue/x_a.gcode")

    @pytest.mark.parametrize("path", ["a.gcode", "multiace_queue/",
                                      "multiace_queue/../x.gcode",
                                      "other/x.gcode"])
    def test_paths_outside_the_queue_folder_are_rejected(self, q, path):
        _, c, _ = q
        assert _add(c, path=path).status_code == 400

    def test_queue_route_is_not_swallowed_by_history(self, q):
        _, c, _ = q
        assert "queue" in c.get("/api/queue").json()


class TestQueueEstimate:
    """The estimate and swap count the preflight showed are kept with the
    queued file, so the Queue tab can show them without opening it."""

    def _post(self, c, **extra):
        return c.post("/api/queue", json={
            "filename": "a.gcode", "path": "multiace_queue/x_a.gcode",
            "mode": "slicer", "used_slots": [[0, 0]], **extra})

    def test_the_estimate_is_stored_and_listed(self, q):
        _, c, _ = q
        r = self._post(c, estimate={"total_s": 7260.4, "base_s": 6000},
                       swaps=12)
        assert r.json()["entry"]["estimate"] == {
            "total_s": 7260.4, "base_s": 6000.0, "swaps": 12}
        listed = c.get("/api/queue").json()["queue"][0]
        assert listed["estimate"]["total_s"] == 7260.4

    def test_an_older_client_without_it_still_queues(self, q):
        _, c, _ = q
        r = self._post(c)
        assert r.status_code == 200
        assert "estimate" not in r.json()["entry"]

    @pytest.mark.parametrize("bad", [
        {"total_s": "soon"}, {"total_s": -5}, {"total_s": 0},
        {"total_s": True}, {"total_s": 1e12}, {"base_s": 100}, "text", [1]])
    def test_junk_is_dropped_not_stored(self, q, bad):
        _, c, _ = q
        r = self._post(c, estimate=bad)
        assert r.status_code == 200
        assert "estimate" not in r.json()["entry"]

    def test_base_without_a_total_is_dropped(self, q):
        main, _, _ = q
        assert main._queue_snapshot({"base_s": 100}, None) is None

    def test_the_swap_count_alone_is_kept(self, q):
        main, _, _ = q
        assert main._queue_snapshot(None, 4) == {"swaps": 4}
        assert main._queue_snapshot(None, -1) is None
        assert main._queue_snapshot(None, True) is None

    def test_listing_never_changes_the_queue(self, q):
        _, c, _ = q
        self._post(c, estimate={"total_s": 100}, swaps=1)
        first = c.get("/api/queue").json()
        again = c.get("/api/queue").json()
        assert first == again and len(again["queue"]) == 1


class TestQueueOriginal:
    """The slicer export kept with an entry (gzip) so Reassign can reopen it,
    and the replace that follows."""

    GZ = __import__("gzip").compress(b"; slicer export\nG1 X1\n")

    def _entry(self, c, **kw):
        assert _add(c, **kw).status_code == 200
        return c.get("/api/queue").json()["queue"][-1]["id"]

    def _put(self, c, qid, body=None):
        return c.put(f"/api/queue/{qid}/original", content=body or self.GZ,
                     headers={"Content-Type": "application/gzip"})

    def test_an_entry_starts_without_an_original(self, q):
        _, c, _ = q
        self._entry(c)
        assert c.get("/api/queue").json()["queue"][0]["has_original"] is False

    def test_the_original_round_trips(self, q):
        _, c, _ = q
        qid = self._entry(c)
        assert self._put(c, qid).status_code == 200
        assert c.get("/api/queue").json()["queue"][0]["has_original"] is True
        got = c.get(f"/api/queue/{qid}/original")
        assert got.status_code == 200 and got.content == self.GZ

    def test_only_gzip_is_accepted(self, q):
        _, c, _ = q
        qid = self._entry(c)
        assert self._put(c, qid, b"not gzip at all").status_code == 400
        assert c.get(f"/api/queue/{qid}/original").status_code == 404

    def test_an_unknown_entry_gets_no_original(self, q):
        _, c, _ = q
        assert self._put(c, "0" * 12).status_code == 404

    def test_a_bad_id_cannot_reach_other_paths(self, q):
        _, c, _ = q
        assert self._put(c, "not-an-id").status_code == 400
        assert c.get("/api/queue/not-an-id/original").status_code == 404

    def test_too_large_is_refused(self, q, monkeypatch):
        main, c, _ = q
        qid = self._entry(c)
        monkeypatch.setattr(main, "_QUEUE_ORIG_MAX", 8)
        assert self._put(c, qid).status_code == 413
        assert c.get(f"/api/queue/{qid}/original").status_code == 404

    def test_remove_drops_the_original(self, q):
        main, c, _ = q
        qid = self._entry(c)
        self._put(c, qid)
        c.delete(f"/api/queue/{qid}")
        assert c.get(f"/api/queue/{qid}/original").status_code == 404

    def test_launch_drops_the_original(self, q):
        _, c, _ = q
        qid = self._entry(c, used_slots=((0, 0),))
        self._put(c, qid)
        assert c.post(f"/api/queue/{qid}/launch").status_code == 200
        assert c.get(f"/api/queue/{qid}/original").status_code == 404

    def test_replacing_removes_the_old_entry_file_and_original(self, q):
        _, c, state = q
        old = self._entry(c, path="multiace_queue/old_a.gcode")
        self._put(c, old)
        r = c.post("/api/queue", json={
            "filename": "a.gcode", "path": "multiace_queue/new_a.gcode",
            "mode": "slicer", "used_slots": [[0, 0]], "replaces": old})
        assert r.status_code == 200
        ids = [e["id"] for e in c.get("/api/queue").json()["queue"]]
        assert ids == [r.json()["entry"]["id"]]
        assert c.get(f"/api/queue/{old}/original").status_code == 404
        assert any(u.endswith("multiace_queue/old_a.gcode")
                   for u in state["deleted"])

    def test_a_failed_add_keeps_the_entry_it_would_replace(self, q, monkeypatch):
        main, c, _ = q
        old = self._entry(c)
        self._put(c, old)
        monkeypatch.setattr(main._require_history(), "MAX_QUEUE", 1)
        r = c.post("/api/queue", json={
            "filename": "a.gcode", "path": "multiace_queue/new_a.gcode",
            "mode": "slicer", "used_slots": [[0, 0]], "replaces": old})
        assert r.status_code == 409
        assert [e["id"] for e in c.get("/api/queue").json()["queue"]] == [old]
        assert c.get(f"/api/queue/{old}/original").status_code == 200

    def test_listing_sweeps_originals_without_an_entry(self, q):
        import pathlib
        main, c, _ = q
        d = pathlib.Path(main.MULTIACE_DATA_DIR) / main._QUEUE_ORIG_DIR
        d.mkdir()
        (d / ("a" * 12 + ".gcode.gz")).write_bytes(self.GZ)
        c.get("/api/queue")
        assert not (d / ("a" * 12 + ".gcode.gz")).exists()


class TestUsedAceSlotsFile:
    def _slots(self, tmp_path, text):
        from multiace.web.backend import preflight_core as pc
        f = tmp_path / "a.gcode"
        f.write_text(text, encoding="utf-8")
        return pc.used_ace_slots_file(str(f))

    def test_reads_ace_and_slot_of_the_swap_lines(self, tmp_path):
        assert self._slots(tmp_path, (
            "ACE_SWAP_HEAD HEAD=0 ACE=0 SLOT=0 INITIAL=1\n"
            "G1 X1\n"
            "ACE_SWAP_HEAD HEAD=1 ACE=1 SLOT=2 ANTI_OOZE=0.5\n"
            "ACE_BG_SWAP HEAD=2 ACE=0 SLOT=3 ANTI_OOZE=0 QUIET=1\n"
        )) == [[0, 0], [0, 3], [1, 2]]

    def test_the_slicers_tool_numbers_are_not_slots(self, tmp_path):
        # T5 would have meant ace1/slot1 under the old T // 4 reading; the
        # rewrite keeps the slicer's own numbers in these comments.
        assert self._slots(tmp_path, (
            "; Change Tool 0 -> Tool 5\n"
            "T5\n"
            "ACE_SWAP_HEAD HEAD=0 ACE=0 SLOT=2\n")) == [[0, 2]]

    def test_skipped_swaps_are_comments_and_do_not_count(self, tmp_path):
        assert self._slots(tmp_path, (
            "ACE_SWAP_HEAD HEAD=0 ACE=0 SLOT=0\n"
            "; ACE_SWAP_HEAD HEAD=0 ACE=1 SLOT=1  ; skipped (already loaded)\n"
        )) == [[0, 0]]

    def test_a_file_without_swaps_has_no_slots(self, tmp_path):
        assert self._slots(tmp_path, "T0\nG1 X1\n") == []
