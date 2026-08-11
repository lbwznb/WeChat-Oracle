import io
import json

from wechat_oracle.llm import PiRpcLLM


class FakeProcess:
    def __init__(self, cmd, **kwargs):
        self.cmd = cmd
        self.stdin = io.BytesIO()
        self.stdout = io.BytesIO(
            b'{"id":"prompt","type":"response","command":"prompt","success":true}\n'
            b'{"type":"agent_settled"}\n'
            b'{"id":"result","type":"response","command":"get_last_assistant_text",'
            b'"success":true,"data":{"text":"ok"}}\n'
        )
        self.returncode = None
        self.pid = 12345

    def poll(self):
        return self.returncode

    def terminate(self):
        self.returncode = 0

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.returncode = -9


def test_pi_rpc_uses_isolated_tool_free_process(monkeypatch) -> None:
    captured = {}

    def factory(cmd, **kwargs):
        proc = FakeProcess(cmd, **kwargs)
        captured["proc"] = proc
        return proc

    monkeypatch.setattr("wechat_oracle.llm.shutil.which", lambda _: "pi.cmd")
    monkeypatch.setattr("wechat_oracle.llm.subprocess.Popen", factory)
    monkeypatch.setattr("wechat_oracle.llm.subprocess.run", lambda *args, **kwargs: None)
    client = PiRpcLLM(provider="opencode-go", model="deepseek-v4-flash", timeout_seconds=2)
    assert client.complete_text(model="ignored", system="system", user="你好") == "ok"
    proc = captured["proc"]
    command_text = " ".join(proc.cmd)
    assert "--no-tools" in command_text
    assert "--no-session" in command_text
    writes = [json.loads(line) for line in proc.stdin.getvalue().splitlines()]
    assert writes[0]["message"] == "你好"
    assert writes[1]["type"] == "get_last_assistant_text"
