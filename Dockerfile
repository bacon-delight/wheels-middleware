# Container-image Lambda for the FastAPI API (deployed to ECR, run via Mangum).
# PyMuPDF ships manylinux wheels, so this builds without extra system packages.
FROM public.ecr.aws/lambda/python:3.13

WORKDIR ${LAMBDA_TASK_ROOT}

# Install dependencies first for better layer caching.
COPY pyproject.toml ./
COPY app ./app
RUN pip install --no-cache-dir ".[api]"

# Mangum entrypoint: module.attribute
CMD ["app.main.handler"]
