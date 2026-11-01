from chemistry_agent.automation.step_data import complete_xdatcar, ionic_history


def test_step_history_ignores_electronic_iterations_and_unflushed_energy() -> None:
    log = "DAV:  12  -19.456\n  1 F= -11 E0= -.1240D+02  d E =0\n 2 F= -11 E0= -12."
    assert ionic_history(log) == [[1, -12.4]]


def test_live_trajectory_keeps_last_complete_frame() -> None:
    first = "Cu\n1\n2 0 0\n0 2 0\n0 0 2\nCu\n2\nDirect configuration= 1\n0 0 0\n.5 .5 .5\n"
    partial = first + "Direct configuration= 2\n0 0 0\n.5 ."
    assert complete_xdatcar(partial, 2) == first
