FROM docker.io/library/python@sha256:a83ba18aef0f865b1a4a611c2cf460fe446f2795673bbb9fe43be570439e3b39

WORKDIR /fixture
RUN --mount=type=bind,source=requirements.lock,target=/tmp/requirements.lock \
    PYTHONHASHSEED=0 python -m pip --isolated install \
    --index-url https://pypi.org/simple --disable-pip-version-check \
    --only-binary=:all: --require-hashes --no-cache-dir --no-compile \
    --requirement /tmp/requirements.lock

ENV PYTHONPATH=/fixture/src PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1
USER 65534:65534
