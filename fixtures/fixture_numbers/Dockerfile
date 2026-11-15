FROM python:3.11.8-slim
WORKDIR /fixture
COPY requirements-test.txt /fixture/requirements-test.txt
RUN pip install --no-cache-dir -r requirements-test.txt
COPY pyproject.toml /fixture/
COPY src /fixture/src
COPY tests /fixture/tests
ENV PYTHONPATH=/fixture/src PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1
USER 65534:65534
CMD ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider"]
