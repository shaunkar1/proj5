# Same base image the commits were labeled in (its Python and SQLite decide
# which sqlglot tests run).
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_ROOT_USER_ACTION=ignore

# The exact package set the commits were labeled with.
COPY requirements.lock /app/requirements.lock
RUN pip install -r /app/requirements.lock \
 && (python -m compileall -q --invalidation-mode checked-hash /usr/local/lib/python3.13/site-packages || true)

# sqlglot at the pinned commit, with bytecode compiled ahead of time (the run is
# read-only).  Bytecode is validated by source hash, not by file time, so it stays
# valid however the image layers are stored and through the symlinked trees.
COPY build/fetch_sqlglot.py /app/build/fetch_sqlglot.py
RUN python /app/build/fetch_sqlglot.py /app/repo \
 && python -m compileall -q --invalidation-mode checked-hash /app/repo/sqlglot /app/repo/tests

COPY app /app/app
COPY maps /app/maps
RUN python -m compileall -q --invalidation-mode checked-hash /app/app && chmod -R a+rX /app

ENV SKIP_INTEGRATION=0 PYTHONDONTWRITEBYTECODE=1 OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 MKL_NUM_THREADS=1
CMD ["python", "/app/app/main.py"]
