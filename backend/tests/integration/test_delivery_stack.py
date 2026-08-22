"""交付栈冒烟测试（T104 / quickstart「质量与交付验证」）。

覆盖三类校验：

- 部署编排静态契约（不需要 Docker）：compose.yaml 的 one-off migrate 串行执行、
  API/worker 共用必填 BACKEND_IMAGE 且启动命令不自动迁移、迁移成功后才切换容器、
  ``/data/orionamesh`` 命名持久卷由 API/worker 共同挂载、前端使用必填 FRONTEND_IMAGE、
  应用端口不发布到公网、部署模式拒绝本地默认值；
- 配置就绪（不需要外部服务）：AUTH_JWT_SECRET_KEY 缺失/过短拒绝就绪；
  MODEL_GATEWAY_ENDPOINT 缺失、非法、非回环 HTTP 拒绝就绪，HTTPS 与本机回环
  HTTP 可报告就绪；
- 锁文件不可变安装契约：根目录唯一 pnpm-lock.yaml（frontend 无独立锁文件）、
  uv.lock 存在，所有安装命令均使用 --locked / --frozen-lockfile；
- 完整 Compose 冒烟（需要 Docker daemon，默认 skip）：``RUN_DELIVERY_SMOKE=1``
  时构建并启动全栈，验证 /health 与 /ready、worker 就绪、容器重建后持久卷内
  文件保留；该步骤在 A6 门禁（T105）本地执行。GitHub Actions 对正式 tag 必须
  导出 linux/amd64 双镜像、打包校验和并创建 Release。
"""

import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest
import yaml
from pydantic_core import ValidationError

from app.core.readiness import validate_config
from app.core.settings import get_settings

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[3]
COMPOSE_FILE = REPO_ROOT / "deploy" / "compose" / "compose.yaml"
_NGINX_CONFIG_MOUNT = (
    "${ORIONAMESH_NGINX_CONFIG:?set ORIONAMESH_NGINX_CONFIG to the installed nginx.conf}"
    ":/etc/nginx/conf.d/default.conf:ro"
)

# Compose 必填变量（与 compose.yaml 的 :? 语法一致；配置校验用占位值）。
_COMPOSE_REQUIRED_ENV = {
    "BACKEND_IMAGE": "orionamesh-backend:sha-test",
    "FRONTEND_IMAGE": "orionamesh-frontend:sha-test",
    "POSTGRES_PASSWORD": "p" * 32,
    "REDIS_PASSWORD": "r" * 32,
    "AUTH_JWT_SECRET_KEY": "x" * 40,
    "RATE_LIMIT_SUBJECT_HMAC_KEY": "y" * 40,
    "RATE_LIMIT_TRUSTED_PROXY_CIDRS": "172.16.0.0/12",
    "MODEL_GATEWAY_ENDPOINT": "https://api.example.com/v1",
    "MODEL_GATEWAY_API_KEY": "gateway-key",
    "MODEL_GATEWAY_QUERY_REWRITE_MODEL": "qw",
    "MODEL_GATEWAY_GENERATION_MODEL": "gen",
    "ORIONAMESH_NGINX_CONFIG": str(REPO_ROOT / "deploy" / "nginx" / "nginx.conf"),
}


def _load_compose() -> dict:
    with COMPOSE_FILE.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


class TestComposeDeploymentContract:
    """部署编排静态契约（T099/T104）。"""

    def test_required_services_present(self) -> None:
        services = _load_compose()["services"]
        assert {"postgres", "redis", "migrate", "api", "worker", "frontend", "nginx"} <= set(
            services
        )

    def test_migrate_is_single_one_off_upgrade(self) -> None:
        migrate = _load_compose()["services"]["migrate"]
        assert migrate.get("restart") == "no"
        command = " ".join(migrate["command"])
        assert "alembic" in command and "upgrade" in command and "head" in command
        # 单次串行：仅 migrate 服务执行迁移，且不启动多余副本。
        assert migrate.get("deploy", {}).get("replicas", 1) == 1

    def test_api_and_worker_share_backend_image(self) -> None:
        compose = _load_compose()
        api = compose["services"]["api"]
        worker = compose["services"]["worker"]
        assert api["image"] == worker["image"]
        assert "${BACKEND_IMAGE:?" in api["image"]
        assert "${FRONTEND_IMAGE:?" in compose["services"]["frontend"]["image"]
        for service in ("api", "worker", "frontend"):
            assert "build" not in compose["services"][service]

    def test_only_nginx_publishes_a_host_port(self) -> None:
        services = _load_compose()["services"]
        assert services["nginx"]["ports"] == ["80:80"]
        for service in ("postgres", "redis", "api", "worker", "frontend", "migrate"):
            assert "ports" not in services[service]

    def test_nginx_mount_uses_an_explicit_host_configuration_path(self) -> None:
        nginx_volume = _load_compose()["services"]["nginx"]["volumes"]
        assert nginx_volume == [_NGINX_CONFIG_MOUNT]

    def test_api_and_worker_do_not_auto_migrate(self) -> None:
        compose = _load_compose()
        for service in ("api", "worker"):
            command = " ".join(compose["services"][service]["command"])
            assert "alembic" not in command, f"{service} 启动命令不得自动迁移"

    def test_api_worker_wait_for_migrate_success(self) -> None:
        compose = _load_compose()
        for service in ("api", "worker"):
            condition = compose["services"][service]["depends_on"]["migrate"]["condition"]
            assert condition == "service_completed_successfully"
            # 迁移失败（migrate 非零退出）时 API/worker 不启动，旧容器保持运行。

    def test_shared_named_volume_mounted_on_api_and_worker(self) -> None:
        compose = _load_compose()
        for service in ("api", "worker"):
            volumes = compose["services"][service]["volumes"]
            assert any("orionamesh-data:/data/orionamesh" in v for v in volumes)
        assert "orionamesh-data" in compose.get("volumes", {})

    def test_deployment_mode_rejects_local_defaults(self) -> None:
        environment = _load_compose()["services"]["api"]["environment"]
        assert environment["APP_ENV"] == "production"
        for var in ("DATABASE_URL", "REDIS_URL", "DOCUMENT_STORAGE_ROOT", "AUTH_JWT_SECRET_KEY"):
            assert var in environment

    def test_backend_image_sets_staging_and_no_env_files(self) -> None:
        dockerfile = (REPO_ROOT / "deploy" / "docker" / "backend.Dockerfile").read_text(
            encoding="utf-8"
        )
        assert "uv sync --locked" in dockerfile
        assert "APP_ENV=staging" in dockerfile
        assert 'CMD ["uvicorn"' in dockerfile
        assert "ghcr.io" not in dockerfile

    def test_frontend_image_uses_frozen_lockfile_and_standalone(self) -> None:
        dockerfile = (REPO_ROOT / "deploy" / "docker" / "frontend.Dockerfile").read_text(
            encoding="utf-8"
        )
        assert "pnpm install --frozen-lockfile" in dockerfile
        assert ".next/standalone" in dockerfile
        assert "ARG NEXT_PUBLIC_API_BASE_URL=/v1" in dockerfile

    def test_tag_workflow_exports_release_bundle(self) -> None:
        workflow = (REPO_ROOT / ".github" / "workflows" / "image.yml").read_text(encoding="utf-8")
        assert "linux/amd64" in workflow
        assert "docker image save" in workflow
        assert "actions/upload-artifact@v4" in workflow
        assert "actions/download-artifact@v4" in workflow
        assert "gh release" in workflow
        assert "sha256sum" in workflow
        # upload-artifact@v4 上传工件要求 job 具备 actions: write；只给 build-and-scan job 授权。
        assert "actions: write" in workflow
        assert "build-args: ${{ matrix.build_args }}" in workflow
        deploy_script = (REPO_ROOT / "scripts" / "deploy.sh").read_text(encoding="utf-8")
        assert "release.files.sha256" in deploy_script
        assert "sha256sum -c" in deploy_script
        assert "docker image load" in deploy_script
        # 应用镜像绝不拉取；基础设施镜像首次部署允许按缺失拉取，且先等待健康。
        assert "--no-build --pull never" in deploy_script
        assert "--pull missing" in deploy_script
        assert "--wait --wait-timeout 300 postgres redis" in deploy_script
        # 迁移用独立 one-off 容器（run --rm --no-deps）：可靠传播退出码且不触碰基础设施
        # 生命周期（--exit-code-from 隐含 --abort-on-container-exit，跨版本停止语义不确定）。
        assert "run --rm --no-deps" in deploy_script
        # .env 不得定义镜像引用；最终插值必须与已校验清单一致。
        assert "不得定义 BACKEND_IMAGE/FRONTEND_IMAGE" in deploy_script
        assert "校验最终镜像引用" in deploy_script
        nginx = (REPO_ROOT / "deploy" / "nginx" / "nginx.conf").read_text(encoding="utf-8")
        assert "resolver 127.0.0.11" in nginx


class TestDeliveryReadiness:
    """交付配置就绪：HS256 密钥与模型网关 endpoint 契约（T104）。"""

    def test_jwt_secret_missing_or_too_short_rejects_ready(self, monkeypatch) -> None:
        monkeypatch.delenv("AUTH_JWT_SECRET_KEY", raising=False)
        get_settings.cache_clear()
        assert any("AUTH_JWT_SECRET_KEY is required" in e for e in validate_config())

        monkeypatch.setenv("AUTH_JWT_SECRET_KEY", "too-short")
        get_settings.cache_clear()
        assert any(
            "AUTH_JWT_SECRET_KEY must be at least 32 UTF-8 bytes" in e for e in validate_config()
        )

    def test_gateway_endpoint_missing_or_invalid_rejects_ready(self, monkeypatch) -> None:
        monkeypatch.delenv("MODEL_GATEWAY_ENDPOINT", raising=False)
        get_settings.cache_clear()
        assert any("MODEL_GATEWAY_ENDPOINT is required" in e for e in validate_config())

        # 非法 endpoint 与非回环 HTTP 在配置构造阶段即拒绝（ModelGatewaySettings 校验）。
        for bad in ("ftp://api.example.com/v1", "not-a-url", "http://api.example.com/v1"):
            monkeypatch.setenv("MODEL_GATEWAY_ENDPOINT", bad)
            get_settings.cache_clear()
            with pytest.raises(ValidationError):
                get_settings()

    def test_https_and_loopback_http_endpoints_ready(self, monkeypatch) -> None:
        for endpoint in ("https://api.example.com/v1", "http://127.0.0.1:19999/v1"):
            monkeypatch.setenv("MODEL_GATEWAY_ENDPOINT", endpoint)
            get_settings.cache_clear()
            assert validate_config() == []


class TestLockfileImmutability:
    """锁文件不可变安装契约（quickstart）。"""

    def test_single_root_lockfile_only(self) -> None:
        assert (REPO_ROOT / "pnpm-lock.yaml").is_file()
        assert not (REPO_ROOT / "frontend" / "pnpm-lock.yaml").exists()
        assert (REPO_ROOT / "backend" / "uv.lock").is_file()

    def test_all_install_commands_frozen(self) -> None:
        check_backend = (REPO_ROOT / "scripts" / "check-backend.sh").read_text(encoding="utf-8")
        assert "uv sync --locked" in check_backend
        check_frontend = (REPO_ROOT / "scripts" / "check-frontend.sh").read_text(encoding="utf-8")
        assert "pnpm install --frozen-lockfile" in check_frontend
        ci = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        assert "--frozen-lockfile" in ci
        frontend_dockerfile = (REPO_ROOT / "deploy" / "docker" / "frontend.Dockerfile").read_text(
            encoding="utf-8"
        )
        assert "pnpm install --frozen-lockfile" in frontend_dockerfile


# ---------------------------------------------------------------------------
# 完整 Compose 冒烟（需要 Docker daemon；默认 skip，A6 门禁 T105 本地执行）
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def docker_ready() -> bool:
    if shutil.which("docker") is None:
        pytest.skip("docker CLI unavailable")
    for args in (["docker", "compose", "version"], ["docker", "info"]):
        result = subprocess.run(
            args, capture_output=True, text=True, encoding="utf-8", errors="replace"
        )
        if result.returncode != 0:
            pytest.skip(f"docker unavailable ({args[0]} {' '.join(args[1:])})")
    return True


def _compose_env() -> dict[str, str]:
    env = os.environ.copy()
    for var, value in _COMPOSE_REQUIRED_ENV.items():
        env.setdefault(var, value)
    env["ORIONAMESH_NGINX_CONFIG"] = str(REPO_ROOT / "deploy" / "nginx" / "nginx.conf")
    return env


def test_compose_config_valid(docker_ready) -> None:
    """docker compose config 可解析（YAML 锚点/插值合法，必填变量由 _compose_env 提供）。"""
    result = subprocess.run(
        [
            "docker",
            "compose",
            "--project-directory",
            str(REPO_ROOT),
            "-f",
            str(COMPOSE_FILE),
            "config",
            "--quiet",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=_compose_env(),
    )
    assert result.returncode == 0, result.stderr


def _compose(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [
            "docker",
            "compose",
            "--project-directory",
            str(REPO_ROOT),
            "-f",
            str(COMPOSE_FILE),
            *args,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",  # Windows 默认 GBK 无法解码 compose 输出中的 UTF-8 注释
        env=_compose_env(),
    )
    if check and result.returncode != 0:
        raise AssertionError(f"docker compose {' '.join(args)} failed:\n{result.stderr}")
    return result


def _wait_for_http(url: str, timeout: float = 120.0) -> None:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=3) as resp:
                if resp.status == 200:
                    return
        except (urllib.error.URLError, OSError) as exc:
            last_error = exc
        time.sleep(2)
    raise AssertionError(f"{url} 未在 {timeout}s 内就绪: {last_error}")


def _build_release_images() -> None:
    """冒烟前预置与 Release manifest 相同名称的镜像；Compose 本身始终 --no-build。"""
    builds = (
        ("orionamesh-backend:sha-test", "deploy/docker/backend.Dockerfile"),
        ("orionamesh-frontend:sha-test", "deploy/docker/frontend.Dockerfile"),
    )
    for tag, dockerfile in builds:
        # dockerfile 必须用绝对路径：docker CLI 把相对路径按进程 cwd 解析，
        # 而本测试可能从仓库根或 backend/ 任一目录启动。
        absolute_dockerfile = str(REPO_ROOT / dockerfile)
        result = subprocess.run(
            [
                "docker",
                "build",
                "--platform",
                "linux/amd64",
                "-t",
                tag,
                "-f",
                absolute_dockerfile,
                str(REPO_ROOT),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if result.returncode != 0:
            raise AssertionError(f"预置 Release 镜像 {tag} 失败:\n{result.stderr}")


def _assert_api_ready(timeout: float = 60.0) -> None:
    """Compose 内探测 API /ready；容器启动到 uvicorn 监听存在窗口，失败需重试。"""
    deadline = time.monotonic() + timeout
    last_error: str | None = None
    while time.monotonic() < deadline:
        result = _compose(
            "exec",
            "-T",
            "api",
            "python",
            "-c",
            "import urllib.request; "
            "urllib.request.urlopen('http://127.0.0.1:8000/ready', timeout=3)",
            check=False,
        )
        if result.returncode == 0:
            return
        last_error = result.stderr
        time.sleep(2)
    raise AssertionError(f"api /ready 未在 {timeout:.0f}s 内就绪: {last_error}")


@pytest.mark.skipif(
    os.environ.get("RUN_DELIVERY_SMOKE") != "1",
    reason="完整 Compose 冒烟由 A6 门禁 T105 显式执行（RUN_DELIVERY_SMOKE=1）",
)
class TestFullStackSmoke:
    """构建并启动全栈：健康/就绪、one-off 迁移、worker 就绪与持久卷保留。"""

    def test_stack_up_ready_and_volume_persists(self, docker_ready) -> None:
        try:
            _build_release_images()
            # 与生产 deploy.sh 一致：基础设施镜像按缺失拉取（首次），应用镜像必须来自
            # _build_release_images 预置的 Release 同名镜像（--pull never）。
            _compose(
                "up",
                "-d",
                "--no-build",
                "--pull",
                "missing",
                "--wait",
                "--wait-timeout",
                "300",
                "postgres",
                "redis",
            )
            # one-off 迁移与生产同形态（run --rm --no-deps）；后续 up 应用服务时
            # depends_on 会再次自动补跑幂等迁移，并验证迁移后基础设施仍健康存活。
            _compose("run", "--rm", "--no-deps", "--pull", "never", "migrate")
            _compose(
                "up",
                "-d",
                "--no-build",
                "--pull",
                "never",
                "--wait",
                "--wait-timeout",
                "300",
                "api",
                "worker",
                "frontend",
            )
            _compose(
                "up",
                "-d",
                "--no-build",
                "--pull",
                "missing",
                "--wait",
                "--wait-timeout",
                "300",
                "nginx",
            )

            # nginx 是唯一主机端口；业务就绪从 Compose 内 API 探测。
            _wait_for_http("http://127.0.0.1/")
            _assert_api_ready()

            # 持久卷：在卷内写探针文件，重建 api 容器后必须仍存在。
            probe = f"delivery-smoke-{int(time.time())}.probe"
            _compose("exec", "-T", "api", "sh", "-c", f"echo ok > /data/orionamesh/{probe}")
            _compose("up", "-d", "--force-recreate", "api")
            _assert_api_ready()
            result = _compose("exec", "-T", "api", "sh", "-c", f"cat /data/orionamesh/{probe}")
            assert result.stdout.strip() == "ok", "容器重建后持久卷内容必须保留"
            _compose("exec", "-T", "api", "rm", f"/data/orionamesh/{probe}")
        finally:
            _compose("down", check=False)
