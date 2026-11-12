# syntax=docker/dockerfile:1
# Two Lambda images from one repo, both linux/arm64:
#   --target api     Django + gunicorn behind the Lambda Web Adapter (HTTP API)
#   --target worker  plain Lambda Python runtime, handler tenders.worker.handler
# Build with infra/scripts/build_push.sh (adds --platform linux/arm64 --provenance=false).

FROM public.ecr.aws/lambda/python:3.12 AS base
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DJANGO_SETTINGS_MODULE=config.settings
WORKDIR ${LAMBDA_TASK_ROOT}
COPY requirements.txt /tmp/requirements.txt
RUN pip install -r /tmp/requirements.txt
COPY backend/ ${LAMBDA_TASK_ROOT}/
# Precompile: /var/task is read-only at runtime, so this is the only chance.
RUN python -m compileall -q ${LAMBDA_TASK_ROOT}
# Last, so a new sha doesn't invalidate the dependency layers.
ARG GIT_SHA=dev
ENV APP_VERSION=${GIT_SHA}

FROM base AS api
COPY --from=public.ecr.aws/awsguru/aws-lambda-adapter:1.1.0 /lambda-adapter /opt/extensions/lambda-adapter
# collectstatic needs settings to import; these placeholders exist only for this RUN.
RUN DJANGO_SECRET_KEY=collectstatic-build-only DATABASE_URL=sqlite:///:memory: \
    python manage.py collectstatic --noinput -v 0
ENV PORT=8080 \
    AWS_LWA_PORT=8080 \
    AWS_LWA_READINESS_CHECK_PATH=/api/health
EXPOSE 8080
# One request per sandbox at a time on Lambda, so one worker. --timeout 0: gunicorn's
# heartbeat would see a frozen-then-thawed sandbox as a hung worker and kill it; the
# Lambda timeout (30 s) is the real limit. Only /tmp is writable on Lambda: no control
# socket (gunicorn 27 puts it under $HOME), heartbeat files in /tmp.
ENTRYPOINT ["gunicorn"]
CMD ["config.wsgi:application", "--bind=0.0.0.0:8080", "--workers=1", "--threads=1", \
     "--timeout=0", "--graceful-timeout=5", "--access-logfile=-", \
     "--no-control-socket", "--worker-tmp-dir=/tmp"]

FROM base AS worker
CMD ["tenders.worker.handler"]
