from app.hello.router import hello


class _Recorder:
    def __init__(self):
        self.sent = []

    async def answer(self, text, **kwargs):
        self.sent.append(text)


async def test_hello_replies_hello():
    recorder = _Recorder()

    await hello(recorder)

    assert recorder.sent == ["hello"]
