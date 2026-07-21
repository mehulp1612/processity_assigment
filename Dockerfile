# Supermarket Ops Agent image.
#
# Pure-Python image: the agent reaches its model over an OpenAI-compatible HTTP
# endpoint, so there is no vendor CLI to install. matplotlib/reportlab/python-pptx
# run headless.

FROM python:3.13-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    MPLBACKEND=Agg

# DejaVu carries the rupee sign (U+20B9); ReportLab's built-in Helvetica does not,
# so without this every amount on a GST invoice renders as a black box.
RUN apt-get update \
    && apt-get install -y --no-install-recommends fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*


WORKDIR /app

COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY . .

EXPOSE 8000

# Wait for Postgres, apply the schema, seed an empty catalogue, then run the CMD.
# Invoked via bash so it works regardless of the file's exec bit (built on Windows).
ENTRYPOINT ["bash", "/app/scripts/entrypoint.sh"]
CMD ["uvicorn", "app.api:app", "--host", "0.0.0.0", "--port", "8000"]
