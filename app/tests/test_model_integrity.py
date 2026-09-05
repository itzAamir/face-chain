import hashlib

from config import settings


EXPECTED_SHA256 = {
    settings.yunet_model: "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4",
    settings.sface_model: "0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79",
}


def test_bundled_model_checksums() -> None:
    for path, expected in EXPECTED_SHA256.items():
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        assert digest == expected
