# Pulled from ECR Public rather than Docker Hub: the same upstream Docker
# Official Image, mirrored by AWS, so base images never come from an external
# registry (and the pull is not subject to Docker Hub rate limits).
FROM public.ecr.aws/docker/library/python:3.13-slim
WORKDIR /app
# Install from the pinned manifest, not a hand-written `pip install a b c` list:
# this image and the AgentCore Runtime zip (deploy/package.sh) run the SAME
# program, so they resolve the same dependency set from the same file. The old
# inline list had already drifted (it carried boto3/pydantic the MCP server
# never imports, and no upper bound on mcp — see the comment in the manifest).
# Copied before the source so a source-only change reuses the install layer.
COPY deploy/requirements.txt /app/deploy/requirements.txt
RUN pip install --no-cache-dir -r /app/deploy/requirements.txt
COPY src/ /app/src/
# Sanctions matcher shared with the screen_sanctions Gateway Lambda
COPY functions/sanctions_matcher.py /app/functions/sanctions_matcher.py
# Shared XRPL tool logic imported by both the MCP server and the Gateway Lambdas
COPY functions/xrpl_core.py /app/functions/xrpl_core.py
# config/ is deliberately NOT copied. It holds config/wallets.json — private
# XRPL wallet seeds — and an image layer is not a trust boundary: anyone who can
# pull the image gets the seeds, and `docker history` keeps them even if a later
# layer deletes the file. .dockerignore keeps it out of the build context too.
COPY data/ /app/data/
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app
ENV XRPL_NETWORK=testnet
ENV XRPL_RPC_URL=https://s.altnet.rippletest.net:51234
ENV MCP_TRANSPORT=streamable-http
# Where wallet material is supposed to come from now that it is not baked in.
# functions/shared.py already loads this exact secret via boto3; the MCP server
# still reads config/wallets.json and therefore has NO wallets in this image
# (signing tools fail with "Wallet not found" until server.py is switched over).
ENV WALLETS_SECRET_ID=xrpl-agentic-payments/wallets
EXPOSE 8000
CMD ["python", "-m", "src.mcp_server.server"]
