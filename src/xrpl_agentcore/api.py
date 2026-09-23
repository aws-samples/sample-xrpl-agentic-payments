"""FastAPI transfer API. Money movement is intentionally absent from these routes."""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Annotated

import boto3
from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from mangum import Mangum

from .domain import Corridor, Quote, TransferProjection
from .preferences import PreferenceMemory, PreferenceSettings
from .repository import (
    Conflict,
    DynamoTransferRepository,
    Expired,
    InMemoryTransferRepository,
    NotFound,
    TransferRepository,
)
from .services import (
    ApproveTransferRequest,
    CorridorService,
    CreateTransferRequest,
    FixturePathQuoteProvider,
    QuoteRequest,
    QuoteService,
    TransferService,
)

DEFAULT_ADDRESS = "rHb9CJAWyB4rj91VRWn96DkukG4bwdtyTh"
DEFAULT_DESTINATION = "r3XJToiKCCndKMi1NWmWhBjLBuwmHZimbg"


class AppServices:
    def __init__(
        self,
        repository: TransferRepository,
        corridors: CorridorService,
        quote_service: QuoteService,
        transfer_service: TransferService,
    ) -> None:
        self.repository = repository
        self.corridors = corridors
        self.quotes = quote_service
        self.transfers = transfer_service


@lru_cache(maxsize=1)
def configured_services() -> AppServices:
    table_name = os.environ.get("TRANSFER_TABLE_NAME", "").strip()
    if table_name:
        table = boto3.resource(
            "dynamodb",
            region_name=os.environ["AWS_DEFAULT_REGION"],
        ).Table(table_name)
        repository: TransferRepository = DynamoTransferRepository(table)
    else:
        repository = InMemoryTransferRepository()
    corridors = CorridorService.from_environment()
    source = os.environ.get("XRPL_EXECUTION_ADDRESS") or DEFAULT_ADDRESS
    payout = os.environ.get("XRPL_PAYOUT_ADDRESS") or DEFAULT_DESTINATION
    merchant = os.environ.get("XRPL_FEE_MERCHANT_ADDRESS") or DEFAULT_DESTINATION
    quotes = QuoteService(
        repository,
        corridors,
        FixturePathQuoteProvider(),
        source_account=source,
        payout_account=payout,
        fee_merchant=merchant,
    )
    return AppServices(repository, corridors, quotes, TransferService(repository))


def authenticated_sub(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
    x_demo_user: Annotated[str | None, Header()] = None,
) -> str:
    del authorization
    event = request.scope.get("aws.event") or {}
    claims = event.get("requestContext", {}).get("authorizer", {}).get("jwt", {}).get("claims", {})
    subject = claims.get("sub")
    if isinstance(subject, str) and subject:
        return subject
    if os.environ.get("ALLOW_DEMO_AUTH", "false").lower() == "true":
        demo_sub = x_demo_user or os.environ.get("DEMO_USER_SUB", "demo-user")
        if demo_sub.strip():
            return demo_sub.strip()
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="authentication required")


def get_services() -> AppServices:
    return configured_services()


def create_app(services: AppServices | None = None) -> FastAPI:
    app = FastAPI(
        title="AgentCore XRPL Cross-Border Transfer API",
        version="0.1.0",
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=os.environ.get("CORS_ORIGINS", "http://localhost:3000").split(","),
        allow_credentials=True,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=[
            "Authorization",
            "Content-Type",
            "Idempotency-Key",
            "X-Demo-User",
            "X-Memory-Session",
        ],
    )

    def app_services_dependency() -> AppServices:
        return services if services is not None else get_services()

    services_dependency = Depends(app_services_dependency)

    @app.exception_handler(NotFound)
    async def handle_not_found(_request: Request, error: NotFound):
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=404, content={"detail": str(error)})

    @app.exception_handler(Conflict)
    async def handle_conflict(_request: Request, error: Conflict):
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=409, content={"detail": str(error)})

    @app.exception_handler(Expired)
    async def handle_expired(_request: Request, error: Expired):
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=409, content={"detail": str(error), "code": "EXPIRED"})

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "healthy", "network": "xrpl-testnet"}

    @app.get("/v1/corridors", response_model=list[Corridor])
    async def list_corridors(
        _owner: Annotated[str, Depends(authenticated_sub)],
        app_services: AppServices = services_dependency,
    ) -> list[Corridor]:
        return app_services.corridors.list()

    @app.post("/v1/quotes", response_model=Quote, status_code=201)
    async def create_quote(
        request: QuoteRequest,
        owner: Annotated[str, Depends(authenticated_sub)],
        app_services: AppServices = services_dependency,
    ) -> Quote:
        try:
            return app_services.quotes.create(owner, request)
        except (KeyError, ValueError) as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @app.post("/v1/transfers", response_model=TransferProjection, status_code=201)
    async def create_transfer(
        request: CreateTransferRequest,
        owner: Annotated[str, Depends(authenticated_sub)],
        app_services: AppServices = services_dependency,
    ) -> TransferProjection:
        transfer = app_services.transfers.create(owner, request.quote_id)
        return TransferProjection.from_transfer(transfer)

    @app.get("/v1/transfers", response_model=list[TransferProjection])
    async def list_transfers(
        owner: Annotated[str, Depends(authenticated_sub)],
        app_services: AppServices = services_dependency,
    ) -> list[TransferProjection]:
        return app_services.transfers.list(owner)

    @app.post("/v1/transfers/{transfer_id}/approve", response_model=TransferProjection)
    async def approve_transfer(
        transfer_id: str,
        approval: ApproveTransferRequest,
        owner: Annotated[str, Depends(authenticated_sub)],
        app_services: AppServices = services_dependency,
    ) -> TransferProjection:
        transfer = app_services.transfers.approve(owner, transfer_id, approval)
        return TransferProjection.from_transfer(transfer)

    @app.get("/v1/transfers/{transfer_id}", response_model=TransferProjection)
    async def get_transfer(
        transfer_id: str,
        owner: Annotated[str, Depends(authenticated_sub)],
        app_services: AppServices = services_dependency,
    ) -> TransferProjection:
        return app_services.transfers.get(owner, transfer_id)

    @app.post("/v1/preferences", response_model=PreferenceSettings)
    async def save_preferences(
        preferences: PreferenceSettings,
        owner: Annotated[str, Depends(authenticated_sub)],
        x_memory_session: Annotated[str | None, Header()] = None,
    ) -> PreferenceSettings:
        memory = PreferenceMemory.from_environment()
        if preferences.memory_opt_in and memory is not None:
            session_id = (x_memory_session or "").strip()
            if len(session_id) < 8 or len(session_id) > 200:
                raise HTTPException(
                    status_code=422,
                    detail="X-Memory-Session must be 8-200 characters",
                )
            memory.remember(
                actor_id=owner,
                session_id=session_id,
                settings=preferences,
            )
        return preferences

    return app


app = create_app()
lambda_handler = Mangum(app, lifespan="off")
