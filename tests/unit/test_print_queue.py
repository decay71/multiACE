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
