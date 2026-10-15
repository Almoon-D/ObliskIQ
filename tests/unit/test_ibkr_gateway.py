"""The Client Portal Gateway transport: pinned install, own certificate, Java gate, login flow.

The real browser login against IBKR's page is exercised only by the live trial
(docs/knowledge/ibkr.md); here the login driver, the Java tools and the Gateway process are fakes.
"""

import datetime as dt
import hashlib
import io
import re
import ssl
import stat
import zipfile
from pathlib import Path
from typing import Any, ClassVar

import httpx
import pytest
import respx
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from pydantic import SecretStr

from obliskiq.config import load_config
from obliskiq.config.schema_ibkr import IbkrConfig
from obliskiq.ibkr.errors import GatewayError, ProfileRefusedError, RecoveryDeferredError
from obliskiq.ibkr.transport import gateway as gateway_module
from obliskiq.ibkr.transport.gateway import GatewayTransport
from obliskiq.ibkr.transport.gateway_install import (
    CONF_FILE,
    MARKER_FILE,
    GatewayInstall,
    ensure_installed,
    existing_install,
    extract_archive,
    patch_conf,
    verify_archive,
)
from obliskiq.ibkr.transport.gateway_java import JavaRuntime, parse_major_version
from obliskiq.ibkr.transport.gateway_login import LoginOutcome, _proof_returned
from obliskiq.ibkr.transport.gateway_process import GatewayProcess
from obliskiq.util.http import RetryPolicy

SHIPPED_CONF = """proxyRemoteSsl: true
proxyRemoteHost: "https://api.ibkr.com"
listenPort: 5000
listenSsl: true
sslCert: "vertx.jks"
sslPwd: "mywebapi"
ips:
  allow:
    - 127.0.0.1
"""

#: What the fake JDK scripts need from this process's environment.
INHERIT = ("PATH", "HOME")


def _archive(extra: dict[str, str] | None = None) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        bundle.writestr("bin/run.sh", "#!/bin/bash\nsleep 30\n")
        bundle.writestr("root/conf.yaml", SHIPPED_CONF)
        for name, text in (extra or {}).items():
            bundle.writestr(name, text)
    return buffer.getvalue()


def _cert_pem() -> bytes:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = dt.datetime.now(dt.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=30))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.PEM)


def _script(path: Path, body: str) -> None:
    path.write_text("#!/bin/bash\n" + body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


@pytest.fixture
def java_home(tmp_path: Path) -> Path:
    """A fake Java home: ``java -version`` says 11; ``keytool -exportcert`` writes a real PEM."""
    home = tmp_path / "jre"
    (home / "bin").mkdir(parents=True)
    pem = tmp_path / "prepared.pem"
    pem.write_bytes(_cert_pem())
    _script(home / "bin" / "java", "echo 'openjdk version \"11.0.32.1\" 2026-08-18' >&2\n")
    _script(
        home / "bin" / "keytool",
        f'echo "$@" >> "{tmp_path / "keytool-args"}"\n'
        f'[ -n "$GATEWAY_KEYSTORE_PASSWORD" ] && echo set >> "{tmp_path / "keytool-env"}"\n'
        'case "$1" in\n'
        '  -genkeypair) for a; do [ "$prev" = "-keystore" ] && : > "$a"; prev=$a; done ;;\n'
        f'  -exportcert) for a; do [ "$prev" = "-file" ] && cp "{pem}" "$a"; prev=$a; done ;;\n'
        "esac\n",
    )
    return home


@pytest.fixture
def ibkr_for(shipped_settings):
    def build(data: bytes, **gateway: Any):
        update = {
            "sha256": hashlib.sha256(data).hexdigest(),
            "size_bytes": len(data),
            **gateway,
        }
        return shipped_settings.ibkr.model_copy(
            update={"gateway": shipped_settings.ibkr.gateway.model_copy(update=update)}
        )

    return build


@pytest.fixture
def policy(shipped_settings):
    return RetryPolicy.from_settings(shipped_settings.http)


# ── the archive ──


def test_an_archive_of_another_size_is_refused(shipped_settings):
    with pytest.raises(GatewayError, match="not the pinned"):
        verify_archive(b"x" * 10, shipped_settings.ibkr)


def test_an_archive_of_the_right_size_and_another_digest_is_refused(ibkr_for):
    data = _archive()
    ibkr = ibkr_for(data)
    tampered = bytearray(data)
    tampered[-1] ^= 1
    with pytest.raises(GatewayError, match="SHA-256"):
        verify_archive(bytes(tampered), ibkr)


def test_the_pinned_archive_is_accepted(ibkr_for):
    data = _archive()
    verify_archive(data, ibkr_for(data))


def test_a_member_escaping_the_root_is_refused(tmp_path):
    archive = tmp_path / "evil.zip"
    archive.write_bytes(_archive({"../escape.txt": "x"}))
    with pytest.raises(GatewayError, match="outside its root"):
        extract_archive(archive, tmp_path / "root")
    assert not (tmp_path / "escape.txt").exists()


def test_the_conf_is_pointed_at_the_generated_keystore_and_port(tmp_path):
    conf = tmp_path / "conf.yaml"
    conf.write_text(SHIPPED_CONF, encoding="utf-8")
    patch_conf(conf, port=5100, keystore="obliskiq.jks", password="pw-abc")
    text = conf.read_text(encoding="utf-8")
    assert 'sslCert: "obliskiq.jks"' in text
    assert 'sslPwd: "pw-abc"' in text
    assert "listenPort: 5100" in text
    assert 'proxyRemoteHost: "https://api.ibkr.com"' in text, "the rest is left as shipped"


def test_a_conf_missing_a_setting_is_refused(tmp_path):
    conf = tmp_path / "conf.yaml"
    conf.write_text("listenPort: 5000\n", encoding="utf-8")
    with pytest.raises(GatewayError, match="sslCert"):
        patch_conf(conf, port=5000, keystore="k.jks", password="p")


@respx.mock
async def test_the_install_downloads_verifies_and_pins_its_own_certificate(
    tmp_path, ibkr_for, java_home, policy
):
    data = _archive()
    ibkr = ibkr_for(data)
    respx.get(ibkr.gateway.download_url).mock(return_value=httpx.Response(200, content=data))
    async with httpx.AsyncClient() as client:
        install = await ensure_installed(
            ibkr,
            tmp_path / "gw",
            client=client,
            policy=policy,
            java=JavaRuntime(java_home, inherit=INHERIT),
        )
    assert (install.root / MARKER_FILE).read_text().strip() == ibkr.gateway.sha256
    conf = (install.root / str(CONF_FILE)).read_text()
    assert 'sslCert: "obliskiq.jks"' in conf
    assert "mywebapi" not in conf
    assert (install.root / str(CONF_FILE)).stat().st_mode & 0o077 == 0, "owner-only"
    context = install.ssl_context()
    assert context.check_hostname is True
    assert context.verify_mode is ssl.CERT_REQUIRED
    assert len(install.spki_pin()) == 44
    assert existing_install(ibkr, tmp_path / "gw") == install
    (password,) = re.findall(r'sslPwd: "([^"]+)"', conf)
    assert password not in (tmp_path / "keytool-args").read_text(), "not on the command line"
    assert (tmp_path / "keytool-env").read_text().split() == ["set", "set"]


@respx.mock
async def test_a_completed_install_is_not_downloaded_again(tmp_path, ibkr_for, java_home, policy):
    data = _archive()
    ibkr = ibkr_for(data)
    route = respx.get(ibkr.gateway.download_url).mock(
        return_value=httpx.Response(200, content=data)
    )
    async with httpx.AsyncClient() as client:
        for _ in range(2):
            await ensure_installed(
                ibkr,
                tmp_path / "gw",
                client=client,
                policy=policy,
                java=JavaRuntime(java_home, inherit=INHERIT),
            )
    assert route.call_count == 1


@respx.mock
async def test_a_failed_download_installs_nothing(tmp_path, ibkr_for, java_home, policy):
    ibkr = ibkr_for(_archive())
    respx.get(ibkr.gateway.download_url).mock(return_value=httpx.Response(404))
    async with httpx.AsyncClient() as client:
        with pytest.raises(GatewayError, match="HTTP 404"):
            await ensure_installed(
                ibkr,
                tmp_path / "gw",
                client=client,
                policy=policy,
                java=JavaRuntime(java_home, inherit=INHERIT),
            )
    assert existing_install(ibkr, tmp_path / "gw") is None


@respx.mock
async def test_a_changed_archive_is_refused_before_it_is_unpacked(
    tmp_path, ibkr_for, java_home, policy
):
    ibkr = ibkr_for(_archive())
    respx.get(ibkr.gateway.download_url).mock(
        return_value=httpx.Response(200, content=_archive({"extra.txt": "new"}))
    )
    async with httpx.AsyncClient() as client:
        with pytest.raises(GatewayError, match="not the pinned"):
            await ensure_installed(
                ibkr,
                tmp_path / "gw",
                client=client,
                policy=policy,
                java=JavaRuntime(java_home, inherit=INHERIT),
            )
    assert not (tmp_path / "gw").exists() or not any((tmp_path / "gw").iterdir())


# ── Java ──


@pytest.mark.parametrize(
    ("output", "major"),
    [
        ('openjdk version "21.0.12.1" 2026-08-18', 21),
        ('openjdk version "11.0.32.1" 2026-08-18', 11),
        ('java version "1.8.0_392"', 8),
        ('openjdk version "17" 2021-09-14', 17),
        ("command not found", None),
    ],
)
def test_the_java_major_version_is_parsed(output, major):
    assert parse_major_version(output) == major


async def test_a_runtime_of_an_untested_version_is_refused(tmp_path):
    home = tmp_path / "jre21"
    (home / "bin").mkdir(parents=True)
    _script(home / "bin" / "java", "echo 'openjdk version \"21.0.12.1\"' >&2\n")
    with pytest.raises(GatewayError, match="version 21"):
        await JavaRuntime(home, inherit=INHERIT).require([11])


async def test_a_runtime_of_a_tested_version_passes(java_home):
    assert await JavaRuntime(java_home, inherit=INHERIT).require([11]) == 11


def test_a_java_home_without_the_tool_is_refused(tmp_path):
    with pytest.raises(GatewayError, match="no keytool"):
        JavaRuntime(tmp_path, inherit=INHERIT).tool("keytool")


def test_child_processes_inherit_only_the_allowed_variables(
    java_home, monkeypatch, shipped_settings
):
    monkeypatch.setenv("OBLISK_IBKR_GATEWAY_PASSWORD", "paper-pass")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "bot-secret")
    monkeypatch.setenv("FRED_API_KEY", "fred-secret")
    monkeypatch.setenv("LANG", "C.UTF-8")
    allowed = tuple(shipped_settings.ibkr.gateway.inherited_environment)
    env = JavaRuntime(java_home, inherit=allowed).environment()
    assert set(env) <= {*allowed, "JAVA_HOME"}
    assert not {"paper-pass", "bot-secret", "fred-secret"} & set(env.values())
    assert env["LANG"] == "C.UTF-8"


def test_the_runtime_comes_first_on_the_launcher_path(java_home):
    env = JavaRuntime(java_home, inherit=INHERIT).environment()
    assert env["JAVA_HOME"] == str(java_home)
    assert env["PATH"].startswith(str(java_home / "bin"))


# ── the process ──


def _install(tmp_path: Path, run: str) -> GatewayInstall:
    root = tmp_path / "install"
    (root / "bin").mkdir(parents=True)
    (root / "root").mkdir()
    _script(root / "bin" / "run.sh", run)
    cert = root / "gateway-cert.pem"
    cert.write_bytes(_cert_pem())
    return GatewayInstall(root=root, cert_path=cert)


READINESS = "/v1/api/tickle"
ROOT = Path(__file__).resolve().parents[2]


def _gateway(address: str, *, startup: float, stop_grace: float | None = None) -> IbkrConfig:
    """The shipped IBKR settings with the Gateway at another address and a short startup."""
    ibkr = load_config(ROOT / "config").settings.ibkr
    update: dict[str, Any] = {"local_address": address, "startup_timeout_seconds": startup}
    if stop_grace is not None:
        update["stop_grace_seconds"] = stop_grace
    return ibkr.model_copy(update={"gateway": ibkr.gateway.model_copy(update=update)})


async def test_a_gateway_already_serving_is_used_and_never_stopped(
    tmp_path, java_home, monkeypatch
):
    process = GatewayProcess(
        _install(tmp_path, "exit 9\n"),
        ibkr=_gateway("https://localhost:59999", startup=2.0),
        readiness_path=READINESS,
        java=JavaRuntime(java_home, inherit=INHERIT),
    )

    async def serving(self):
        return True

    monkeypatch.setattr(GatewayProcess, "answering", serving)
    await process.start()
    assert not process.started_here
    await process.stop()


async def test_a_gateway_that_exits_while_starting_is_reported(tmp_path, java_home):
    process = GatewayProcess(
        _install(tmp_path, "exit 3\n"),
        ibkr=_gateway("https://localhost:59998", startup=5.0),
        readiness_path=READINESS,
        java=JavaRuntime(java_home, inherit=INHERIT),
    )
    with pytest.raises(GatewayError, match="exited with status 3"):
        await process.start()


async def test_a_gateway_that_never_answers_times_out_and_is_stopped(tmp_path, java_home):
    process = GatewayProcess(
        _install(tmp_path, "sleep 30\n"),
        ibkr=_gateway("https://localhost:59997", startup=1.5),
        readiness_path=READINESS,
        java=JavaRuntime(java_home, inherit=INHERIT),
    )
    with pytest.raises(GatewayError, match="did not answer"):
        await process.start()
    assert not process.started_here


async def test_a_started_gateway_is_stopped_with_its_process_group(
    tmp_path, java_home, monkeypatch
):
    process = GatewayProcess(
        _install(tmp_path, "sleep 30\n"),
        ibkr=_gateway("https://localhost:59996", startup=5.0),
        readiness_path=READINESS,
        java=JavaRuntime(java_home, inherit=INHERIT),
    )
    calls = {"n": 0}

    async def after_launch(self):
        calls["n"] += 1
        return calls["n"] > 1

    monkeypatch.setattr(GatewayProcess, "answering", after_launch)
    await process.start()
    assert process.started_here
    await process.stop()
    assert not process.started_here


async def test_a_probe_through_the_pinned_certificate_fails_closed(tmp_path, java_home):
    process = GatewayProcess(
        _install(tmp_path, "exit 0\n"),
        ibkr=_gateway("https://localhost:1", startup=1.0),
        readiness_path=READINESS,
        java=JavaRuntime(java_home, inherit=INHERIT),
    )
    assert await process.answering() is False


# ── the login driver's verdict ──


@pytest.mark.parametrize(
    ("body", "verdict"),
    [
        ('{"M2": "abc", "reached_max_login": false}', True),
        ('{"M2": "", "reached_max_login": false}', False),
        ('{"reached_max_login": true}', False),
        ('{"B": "x", "s": "y"}', None),
        ("<html>", None),
        ("[1, 2]", None),
    ],
)
def test_the_authenticator_answer_says_whether_the_password_was_accepted(body, verdict):
    assert _proof_returned(body) is verdict


# ── the transport ──


class FakeDriver:
    def __init__(self, outcomes: list[LoginOutcome]) -> None:
        self.outcomes = outcomes
        self.calls: list[dict[str, Any]] = []

    async def login(self, **kwargs: Any) -> LoginOutcome:
        self.calls.append(kwargs)
        return self.outcomes.pop(0)


class FakeProcess:
    instances: ClassVar[list[FakeProcess]] = []

    def __init__(self, install, *, ibkr, readiness_path, java) -> None:
        self.readiness_path = readiness_path
        self.serving = False
        self.starts = 0
        self.stops = 0
        FakeProcess.instances.append(self)

    async def answering(self) -> bool:
        return self.serving

    async def start(self) -> None:
        self.starts += 1
        self.serving = True

    async def stop(self) -> None:
        self.stops += 1


@pytest.fixture
def transport_for(shipped_settings, tmp_path, monkeypatch, policy):
    FakeProcess.instances = []
    install = _install(tmp_path, "exit 0\n")

    async def installed(*args, **kwargs):
        return install

    async def java_ok(self, allowed):
        return 11

    monkeypatch.setattr(gateway_module, "ensure_installed", installed)
    monkeypatch.setattr(gateway_module, "GatewayProcess", FakeProcess)
    monkeypatch.setattr(JavaRuntime, "require", java_ok)

    def build(
        driver: FakeDriver,
        sessions: list[bool],
        profile: str = "paper",
        clock: list[float] | None = None,
    ) -> GatewayTransport:
        now = clock if clock is not None else [0.0]
        transport = GatewayTransport(
            shipped_settings.ibkr,
            profile=profile,  # type: ignore[arg-type]
            username=SecretStr("paper-user"),
            password=SecretStr("paper-pass"),
            base_path=shipped_settings.ibkr.base_path,
            install_dir=tmp_path / "gw",
            policy=policy,
            timeout_seconds=1.0,
            driver=driver,
            monotonic=lambda: now[0],
        )
        answers = list(sessions)

        async def logged_in(self, install):
            return answers.pop(0) if answers else False

        monkeypatch.setattr(GatewayTransport, "_logged_in", logged_in)
        return transport

    return build


def test_profile_live_is_refused(transport_for):
    with pytest.raises(ProfileRefusedError, match="paper-only"):
        transport_for(FakeDriver([]), [], profile="live")


def test_the_transport_is_not_used_before_it_is_prepared(transport_for):
    with pytest.raises(GatewayError, match="before prepare"):
        transport_for(FakeDriver([]), []).verify()


async def test_a_gateway_already_logged_in_needs_no_login(transport_for):
    driver = FakeDriver([])
    transport = transport_for(driver, [True])
    await transport.prepare()
    assert driver.calls == []
    assert isinstance(transport.verify(), ssl.SSLContext)
    assert transport.base_url == "https://localhost:5000/v1/api"
    assert await transport.headers("GET", "x", {}) == {"Accept": "application/json"}


async def test_a_logged_out_gateway_is_started_and_logged_in(transport_for, shipped_settings):
    driver = FakeDriver([LoginOutcome(True, True, "logged in")])
    transport = transport_for(driver, [False, True])
    await transport.prepare()
    assert FakeProcess.instances[0].starts == 1
    (call,) = driver.calls
    assert call["username"] == "paper-user"
    assert call["url"].endswith(shipped_settings.ibkr.gateway.login.route)
    assert call["ibkr"] is shipped_settings.ibkr


async def test_refused_credentials_are_not_retried(transport_for):
    driver = FakeDriver([LoginOutcome(False, False, "the login did not succeed (bad)")] * 3)
    transport = transport_for(driver, [False])
    with pytest.raises(GatewayError, match="headless login failed"):
        await transport.prepare()
    assert len(driver.calls) == 1, "a refused password is never repeated"


async def test_accepted_credentials_without_a_session_are_retried_then_refused(
    transport_for, shipped_settings
):
    outcome = LoginOutcome(False, True, "IBKR accepted the credentials but no session")
    attempts = shipped_settings.ibkr.gateway.login.max_attempts
    driver = FakeDriver([outcome] * attempts)
    transport = transport_for(driver, [False] * (attempts + 1))
    with pytest.raises(GatewayError, match="accepted the credentials"):
        await transport.prepare()
    assert len(driver.calls) == attempts


async def test_a_login_page_success_without_a_session_is_not_success(transport_for):
    driver = FakeDriver([LoginOutcome(True, True, "logged in")] * 2)
    transport = transport_for(driver, [False, False, False])
    with pytest.raises(GatewayError, match="no session after login"):
        await transport.prepare()


async def test_recover_logs_in_again_and_close_stops_the_process(transport_for):
    driver = FakeDriver([LoginOutcome(True, True, "logged in")])
    transport = transport_for(driver, [True, False, True])
    await transport.prepare()
    await transport.recover()
    assert len(driver.calls) == 1
    await transport.aclose()
    assert FakeProcess.instances[0].stops == 1


async def test_a_relogin_inside_the_interval_is_refused_without_a_login(
    transport_for, shipped_settings
):
    """In the cloud failure mode (password accepted, no session) a repair loop would log in
    every few minutes; logins are spaced so repeats cannot lock the username."""
    interval = shipped_settings.ibkr.gateway.login.min_relogin_interval_seconds
    clock = [0.0]
    driver = FakeDriver([LoginOutcome(True, True, "logged in")] * 2)
    transport = transport_for(driver, [False, True, False, False, True], clock=clock)
    await transport.prepare()
    clock[0] = interval / 2
    with pytest.raises(RecoveryDeferredError, match="the next is allowed in"):
        await transport.recover()
    assert len(driver.calls) == 1
    clock[0] = interval
    await transport.recover()
    assert len(driver.calls) == 2


async def test_a_login_browser_that_crashes_is_a_failed_login(transport_for):
    class CrashingDriver(FakeDriver):
        async def login(self, **kwargs: Any) -> LoginOutcome:
            self.calls.append(kwargs)
            raise RuntimeError("chromium crashed")

    transport = transport_for(CrashingDriver([]), [False])
    with pytest.raises(GatewayError, match="the login browser failed: RuntimeError"):
        await transport.prepare()


def test_the_gateway_probes_the_configured_api_path(transport_for, shipped_settings):
    transport = transport_for(FakeDriver([]), [])
    assert transport._probe_path == f"{shipped_settings.ibkr.base_path}/tickle"


async def test_the_password_never_reaches_an_error(transport_for):
    driver = FakeDriver([LoginOutcome(False, False, "the login did not succeed (x)")])
    transport = transport_for(driver, [False])
    with pytest.raises(GatewayError) as caught:
        await transport.prepare()
    assert "paper-pass" not in str(caught.value)
    assert "paper-user" not in str(caught.value)


# ── probes and lookups ──


@respx.mock
async def test_the_session_probe_reads_200_as_logged_in(tmp_path, shipped_settings, policy):
    install = _install(tmp_path, "exit 0\n")
    transport = GatewayTransport(
        shipped_settings.ibkr,
        profile="paper",
        username=SecretStr("u"),
        password=SecretStr("p"),
        base_path=shipped_settings.ibkr.base_path,
        install_dir=tmp_path / "gw",
        policy=policy,
        timeout_seconds=1.0,
        driver=FakeDriver([]),
    )
    route = respx.post("https://localhost:5000/v1/api/tickle")
    route.return_value = httpx.Response(200, json={})
    assert await transport._logged_in(install) is True
    route.return_value = httpx.Response(401)
    assert await transport._logged_in(install) is False
    route.side_effect = httpx.ConnectError("refused")
    assert await transport._logged_in(install) is False


@respx.mock
async def test_a_serving_gateway_answers_the_readiness_probe(tmp_path, java_home):
    process = GatewayProcess(
        _install(tmp_path, "exit 0\n"),
        ibkr=_gateway("https://localhost:59995", startup=1.0),
        readiness_path=READINESS,
        java=JavaRuntime(java_home, inherit=INHERIT),
    )
    respx.get("https://localhost:59995/v1/api/tickle").mock(return_value=httpx.Response(401))
    assert await process.answering() is True


async def test_a_gateway_ignoring_sigterm_is_killed(tmp_path, java_home, monkeypatch):
    process = GatewayProcess(
        _install(tmp_path, "trap '' TERM\nwhile true; do sleep 0.1; done\n"),
        ibkr=_gateway("https://localhost:59994", startup=5.0, stop_grace=0.5),
        readiness_path=READINESS,
        java=JavaRuntime(java_home, inherit=INHERIT),
    )
    calls = {"n": 0}

    async def after_launch(self):
        calls["n"] += 1
        return calls["n"] > 2

    monkeypatch.setattr(GatewayProcess, "answering", after_launch)
    await process.start()
    await process.stop()
    assert not process.started_here


def test_java_on_the_path_is_used_when_no_home_is_set(monkeypatch):
    from obliskiq.ibkr.transport import gateway_java

    monkeypatch.setattr(gateway_java.shutil, "which", lambda name: f"/usr/bin/{name}")
    assert JavaRuntime(None, inherit=INHERIT).tool("keytool") == "/usr/bin/keytool"
    monkeypatch.setattr(gateway_java.shutil, "which", lambda _name: None)
    with pytest.raises(GatewayError, match="keytool was not found"):
        JavaRuntime(None, inherit=INHERIT).tool("keytool")


async def test_an_injected_download_client_is_used(transport_for, monkeypatch, tmp_path):
    seen: dict[str, Any] = {}

    async def installed(*args, **kwargs):
        seen.update(kwargs)
        return _install(tmp_path / "other", "exit 0\n")

    monkeypatch.setattr(gateway_module, "ensure_installed", installed)
    transport = transport_for(FakeDriver([]), [True])
    async with httpx.AsyncClient() as client:
        transport._download_client = client
        await transport.prepare()
        assert seen["client"] is client


# ── the read-only preflight through the transport it builds itself (Session 4) ──


async def test_the_preflight_builds_and_logs_in_its_own_gateway(
    transport_for, shipped_settings, tmp_path, monkeypatch
):
    from read_path import mock_read_path

    from obliskiq.env import Environment
    from obliskiq.ibkr.preflight import READ_STEPS, run_read_only

    # Installs the fakes (install, process, Java, the session probe); the walk builds its own
    # transport. The probe answers: logged out, then logged in after the login, and still
    # logged in when the session opens.
    transport_for(FakeDriver([]), [False, True, True])
    for name, value in {
        "OBLISK_IBKR_PROFILE": "paper",
        "OBLISK_IBKR_ACCOUNT_ID": "DU_PAPER",
        "OBLISK_IBKR_TRANSPORT": "gateway",
        "OBLISK_IBKR_GATEWAY_USERNAME": "paper-user",
        "OBLISK_IBKR_GATEWAY_PASSWORD": "paper-pass",
    }.items():
        monkeypatch.setenv(name, value)
    marketdata = shipped_settings.ibkr.marketdata.model_copy(
        update={"snapshot_settle_delay_seconds": 0.0}
    )
    settings = shipped_settings.model_copy(
        update={"ibkr": shipped_settings.ibkr.model_copy(update={"marketdata": marketdata})}
    )
    driver = FakeDriver([LoginOutcome(True, True, "logged in")])
    base = shipped_settings.ibkr.gateway.local_address + shipped_settings.ibkr.base_path
    with respx.mock(assert_all_called=False) as router:
        mock_read_path(router, base=base, account="DU_PAPER")
        report = await run_read_only(
            Environment(_env_file=None), settings, data_dir=tmp_path, login_driver=driver
        )
    assert report.ok, report.render()
    assert [step.name for step in report.steps] == list(READ_STEPS)
    assert len(driver.calls) == 1, "the injected driver logged the built Gateway in"
    assert FakeProcess.instances[-1].starts == 1
    assert FakeProcess.instances[-1].stops == 1, "the Gateway this run started is stopped"
