@echo off
cd /d "%~dp0.."
set TARGET=%*
if "%TARGET%"=="" set TARGET=tests
docker build -t radarauditor-worker-test:base . > work\docker_build.log 2>&1
if errorlevel 1 exit /b 1
docker build -t radarauditor-worker-test:pg -f scripts\Dockerfile.tests --build-arg BASE=radarauditor-worker-test:base scripts >> work\docker_build.log 2>&1
if errorlevel 1 exit /b 1
docker run --rm --name radarauditor-tests -v "%CD%\pyproject.toml:/src/pyproject.toml:ro" -v "%CD%\uv.lock:/src/uv.lock:ro" -v "%CD%\README.md:/src/README.md:ro" -v "%CD%\src:/src/src:ro" -v "%CD%\tests:/src/tests:ro" -v "%CD%\config:/opt/radarauditor/config:ro" -v "%CD%\supabase:/src/supabase:ro" -v "%CD%\.env.example:/src/.env.example:ro" -v "%CD%\work\belege_dataforseo_2026-11-13:/belege_dataforseo:ro" -v "%CD%\work:/out" -e UV_PROJECT_ENVIRONMENT=/tmp/venv -e UV_CACHE_DIR=/tmp/uvcache -e UV_LINK_MODE=copy -e PYTHONPATH=/src/src -e PYTHONDONTWRITEBYTECODE=1 -w /tmp --entrypoint bash radarauditor-worker-test:pg -c "mkdir -p /tmp/proj/work && ln -s /belege_dataforseo /tmp/proj/work/belege_dataforseo_2026-11-13 && cp /src/pyproject.toml /src/uv.lock /src/README.md /tmp/proj/ && cd /tmp/proj && uv sync --frozen --extra dev --no-install-project && cp -r /src/tests /src/supabase /src/.env.example /tmp/proj/ && cp -r /opt/radarauditor/config /opt/radarauditor/templates /opt/radarauditor/skills /opt/radarauditor/SKILL.md /tmp/proj/ && cp -r /opt/radarauditor/.claude /tmp/proj/.claude && cd /tmp/proj && /tmp/venv/bin/python -m pytest -q -rs --tb=short -p no:cacheprovider %TARGET%" > work\docker_pytest.log 2>&1
