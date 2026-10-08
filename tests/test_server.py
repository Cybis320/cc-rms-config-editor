import threading
import urllib.request

from config_editor import server, storage
from config_editor.fleet import Fleet, discover

from test_configfile import make_stations


def _quiet_storage(monkeypatch):
    # Keep serve() from measuring the real ~/RMS_data in the background.
    monkeypatch.setattr(storage, "measure_in_background", lambda fleet, force=False: False)
    monkeypatch.setattr(storage, "measuring", lambda: False)


def _start(monkeypatch, tmp_path, idle_s):
    _quiet_storage(monkeypatch)
    fleet = Fleet.load(discover(make_stations(tmp_path, n=1), tmp_path / "no-rms"), tmp_path / "no-rms")
    httpds = []
    real = server.ThreadingHTTPServer

    def capture(*a, **kw):
        httpds.append(real(*a, **kw))
        return httpds[-1]
    monkeypatch.setattr(server, "ThreadingHTTPServer", capture)
    t = threading.Thread(target=server.serve, args=(fleet, "127.0.0.1", 0), kwargs={"idle_s": idle_s}, daemon=True)
    t.start()
    while not httpds:
        pass
    return t, httpds[0].server_address[1]


def test_server_stops_when_no_page_asks(monkeypatch, tmp_path):
    t, _ = _start(monkeypatch, tmp_path, idle_s=0.4)
    t.join(timeout=5)
    assert not t.is_alive()


def test_requests_keep_server_up(monkeypatch, tmp_path):
    t, port = _start(monkeypatch, tmp_path, idle_s=1.0)
    for _ in range(8):   # 2 s of a page polling every 0.25 s: twice the idle window
        urllib.request.urlopen("http://127.0.0.1:%d/api/alive" % port, timeout=2).read()
        t.join(timeout=0.25)
    assert t.is_alive()
    t.join(timeout=5)    # and once the polling stops, so does the server
    assert not t.is_alive()
