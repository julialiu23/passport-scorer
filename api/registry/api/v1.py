import logging
from typing import List

# --- Deduplication Modules
from account.models import Account, Community, Nonce
from django.shortcuts import get_object_or_404
from ninja import Router
from ninja.pagination import paginate
from registry.models import Passport, Score, Stamp
from registry.permissions import ResearcherPermission
from registry.utils import (
    decode_cursor,
    encode_cursor,
    get_signer,
    get_signing_message,
    permissions_required,
    reverse_lazy_with_query,
)

from ..exceptions import (
    InvalidCommunityScoreRequestException,
    InvalidLimitException,
    InvalidNonceException,
    InvalidSignerException,
    api_get_object_or_404,
)
from ..tasks import score_passport
from .base import ApiKey, check_rate_limit, community_requires_signature, get_scorer_id
from .schema import (
    CursorPaginatedScoreResponse,
    CursorPaginatedStampCredentialResponse,
    DetailedScoreResponse,
    ErrorMessageResponse,
    SigningMessageResponse,
    SubmitPassportPayload,
)

log = logging.getLogger(__name__)
# api = NinjaExtraAPI(urls_namespace="registry")
router = Router()

analytics_router = Router()


@router.get(
    "/signing-message",
    auth=ApiKey(),
    response={
        200: SigningMessageResponse,
        401: ErrorMessageResponse,
        400: ErrorMessageResponse,
    },
    summary="Submit passport for scoring",
    description="""Use this API to get a message to sign and a nonce to use when submitting your passport for scoring.""",
)
def signing_message(request) -> SigningMessageResponse:
    check_rate_limit(request)
    nonce = Nonce.create_nonce().nonce
    return {
        "message": get_signing_message(nonce),
        "nonce": nonce,
    }


@router.post(
    "/submit-passport",
    auth=ApiKey(),
    response={
        200: DetailedScoreResponse,
        401: ErrorMessageResponse,
        400: ErrorMessageResponse,
        404: ErrorMessageResponse,
    },
    summary="Submit passport for scoring",
    description="""Use this API to submit your passport for scoring.\n
This API will return a `DetailedScoreResponse` structure with status **PROCESSING** immediatly while your passport is being pulled from storage and the scoring algorithm is run.\n
You need to check for the status of the operation by calling the `/score/{int:scorer_id}/{str:address}` API. The operation will have finished when the status returned is **DONE**
""",
)
def submit_passport(request, payload: SubmitPassportPayload) -> DetailedScoreResponse:
    check_rate_limit(request)

    # Get DID from address
    # did = get_did(payload.address)
    log.debug("/submit-passport, payload=%s", payload)

    account = request.auth

    return handle_submit_passport(payload, account)


def handle_submit_passport(
    payload: SubmitPassportPayload, account: Account
) -> DetailedScoreResponse:
    address_lower = payload.address.lower()

    try:
        scorer_id = get_scorer_id(payload)
    except Exception as e:
        raise e

    # Get community object
    user_community = get_object_or_404(Community, id=scorer_id, account=account)

    # Verify the signer
    if payload.signature or community_requires_signature(user_community):
        if get_signer(payload.nonce, payload.signature).lower() != address_lower:
            raise InvalidSignerException()

        # Verify nonce
        if not Nonce.use_nonce(payload.nonce):
            log.error("Invalid nonce %s for address %s", payload.nonce, payload.address)
            raise InvalidNonceException()

    # Create an empty passport instance, only needed to be able to create a pending Score
    # The passport will be updated by the score_passport task
    db_passport, _ = Passport.objects.update_or_create(
        address=payload.address.lower(),
        community=user_community,
    )

    # Create a score with status PROCESSING
    score, _ = Score.objects.update_or_create(
        passport_id=db_passport.pk,
        defaults=dict(score=None, status=Score.Status.PROCESSING),
    )

    score_passport.delay(user_community.pk, payload.address)

    return DetailedScoreResponse(
        address=score.passport.address,
        score=score.score,
        status=score.status,
        evidence=score.evidence,
        last_score_timestamp=score.last_score_timestamp.isoformat()
        if score.last_score_timestamp
        else None,
    )


@router.get(
    "/score/{int:scorer_id}/{str:address}",
    auth=ApiKey(),
    response={
        200: DetailedScoreResponse,
        401: ErrorMessageResponse,
        400: ErrorMessageResponse,
        404: ErrorMessageResponse,
    },
    summary="Get score for an address that is associated with a scorer",
    description="""Use this endpoint to fetch the score for a specific address that is associated with a scorer\n
This endpoint will return a `DetailedScoreResponse`. This endpoint will also return the status of the asynchronous operation that was initiated with a request to the `/submit-passport` API.\n
""",
)
def get_score(request, address: str, scorer_id: int) -> DetailedScoreResponse:
    check_rate_limit(request)
    account = request.auth
    return handle_get_score(address, scorer_id, account)


def handle_get_score(
    address: str, scorer_id: int, account: Account
) -> DetailedScoreResponse:
    # Get community object
    user_community = api_get_object_or_404(Community, id=scorer_id, account=account)

    try:
        lower_address = address.lower()

        score = Score.objects.get(
            passport__address=lower_address, passport__community=user_community
        )
        return score
    except Exception as e:
        log.error(
            "Error getting passport scores. scorer_id=%s",
            scorer_id,
            exc_info=True,
        )
        raise InvalidCommunityScoreRequestException() from e


@router.get(
    "/score/{int:scorer_id}",
    auth=ApiKey(),
    response={
        200: List[DetailedScoreResponse],
        401: ErrorMessageResponse,
        400: ErrorMessageResponse,
        404: ErrorMessageResponse,
    },
    summary="Get scores for all addresses that are associated with a scorer",
    description="""Use this endpoint to fetch the scores for all addresses that are associated with a scorer\n
This API will return a list of `DetailedScoreResponse` objects. The endpoint supports pagination and will return a maximum of 1000 scores per request.\n
Pass a limit and offset query parameter to paginate the results. For example: `/score/1?limit=100&offset=100` will return the second page of 100 scores.\n
""",
)
@paginate(pass_parameter="pagination_info")
def get_scores(
    request, scorer_id: int, address: str = "", **kwargs
) -> List[DetailedScoreResponse]:
    check_rate_limit(request)
    if kwargs["pagination_info"].limit > 1000:
        raise InvalidLimitException()

    # Get community object
    user_community = api_get_object_or_404(
        Community, id=scorer_id, account=request.auth
    )

    try:
        scores = Score.objects.filter(
            passport__community__id=user_community.id
        ).select_related("passport")

        if address:
            scores = scores.filter(passport__address=address.lower())

        return scores

    except Exception as e:
        log.error(
            "Error getting passport scores. scorer_id=%s",
            scorer_id,
            exc_info=True,
        )
        raise e


@router.get(
    "/stamps/{str:address}",
    auth=ApiKey(),
    response={
        200: CursorPaginatedStampCredentialResponse,
        400: ErrorMessageResponse,
        401: ErrorMessageResponse,
    },
    summary="Get passport for an address",
    description="""Use this endpoint to fetch the passport for a specific address\n
This endpoint will return a `CursorPaginatedStampCredentialResponse`.\n
""",
)
def get_passport_stamps(
    request, address: str, token: str = None, limit: int = 1000
) -> CursorPaginatedStampCredentialResponse:
    check_rate_limit(request)

    if limit > 1000:
        raise InvalidLimitException()

    # ref: https://medium.com/swlh/how-to-implement-cursor-pagination-like-a-pro-513140b65f32

    query = (
        Stamp.objects.order_by("-id")
        .filter(passport__address=address.lower())
        .select_related("passport")
    )

    direction, id = decode_cursor(token) if token else (None, None)

    if direction == "next":
        # note we use lt here because we're querying in descending order
        stamps = list(query.filter(id__lt=id)[:limit])

    elif direction == "prev":
        stamps = list(query.filter(id__gt=id).order_by("id")[:limit])
        stamps.reverse()

    else:
        stamps = list(query[:limit])

    has_more_stamps = has_prev_stamps = False

    if stamps:
        next_id = stamps[-1].id
        prev_id = stamps[0].id

        has_more_stamps = query.filter(id__lt=next_id).exists()
        has_prev_stamps = query.filter(id__gt=prev_id).exists()

    stamps = [{"version": "1.0.0", "credential": stamp.credential} for stamp in stamps]

    domain = request.build_absolute_uri("/")[:-1]

    next_url = (
        f"""{domain}{reverse_lazy_with_query(
            "registry:get_passport_stamps",
            args=[address],
            query_kwargs={"token": encode_cursor("next", next_id), "limit": limit},
        )}"""
        if has_more_stamps
        else None
    )

    prev_url = (
        f"""{domain}{reverse_lazy_with_query(
            "registry:get_passport_stamps",
            args=[address],
            query_kwargs={"token": encode_cursor("prev", prev_id), "limit": limit},
        )}"""
        if has_prev_stamps
        else None
    )

    response = CursorPaginatedStampCredentialResponse(
        next=next_url, prev=prev_url, items=stamps
    )

    return response


@analytics_router.get("/score/", auth=ApiKey(), response=CursorPaginatedScoreResponse)
@permissions_required([ResearcherPermission])
def get_scores_analytics(
    request, token: str = None, limit: int = 1000
) -> CursorPaginatedScoreResponse:
    if limit > 1000:
        raise InvalidLimitException()

    query = Score.objects.order_by("id").select_related("passport")

    direction, id = decode_cursor(token) if token else (None, None)

    if direction == "next":
        scores = list(query.filter(id__gt=id)[:limit])
    elif direction == "prev":
        scores = list(query.filter(id__lt=id).order_by("-id")[:limit])
        scores.reverse()
    else:
        scores = list(query[:limit])

    has_more_scores = has_prev_scores = False

    if scores:
        next_id = scores[-1].id
        prev_id = scores[0].id

        has_more_scores = query.filter(id__gt=next_id).exists()
        has_prev_scores = query.filter(id__lt=prev_id).exists()

    domain = request.build_absolute_uri("/")[:-1]

    next_url = (
        f"""{domain}{reverse_lazy_with_query(
            "analytics:get_scores_analytics",
            query_kwargs={"token": encode_cursor("next", next_id), "limit": limit},
        )}"""
        if has_more_scores
        else None
    )

    prev_url = (
        f"""{domain}{reverse_lazy_with_query(
            "analytics:get_scores_analytics",
            query_kwargs={"token": encode_cursor("prev", prev_id), "limit": limit},
        )}"""
        if has_prev_scores
        else None
    )

    response = CursorPaginatedScoreResponse(next=next_url, prev=prev_url, items=scores)

    return response


@analytics_router.get(
    "/score/{int:scorer_id}", auth=ApiKey(), response=CursorPaginatedScoreResponse
)
@permissions_required([ResearcherPermission])
def get_scores_by_community_id_analytics(
    request,
    scorer_id: int,
    address: str = "",
    token: str = None,
    limit: int = 1000,
) -> CursorPaginatedScoreResponse:
    if limit > 1000:
        raise InvalidLimitException()

    user_community = api_get_object_or_404(Community, id=scorer_id)

    query = (
        Score.objects.order_by("id")
        .filter(passport__community__id=user_community.id)
        .select_related("passport")
    )

    if address:
        query = query.filter(passport__address=address.lower())

    direction, id = decode_cursor(token) if token else (None, None)

    if direction == "next":
        scores = list(query.filter(id__gt=id)[:limit])
    elif direction == "prev":
        scores = list(query.filter(id__lt=id).order_by("-id")[:limit])
        scores.reverse()
    else:
        scores = list(query[:limit])

    has_more_scores = has_prev_scores = False

    if scores:
        next_id = scores[-1].id
        prev_id = scores[0].id

        has_more_scores = query.filter(id__gt=next_id).exists()
        has_prev_scores = query.filter(id__lt=prev_id).exists()

    domain = request.build_absolute_uri("/")[:-1]

    next_url = (
        f"""{domain}{reverse_lazy_with_query(
            "analytics:get_scores_by_community_id_analytics",
            args=[scorer_id],
            query_kwargs={"token": encode_cursor("next", next_id), "limit": limit},
        )}"""
        if has_more_scores
        else None
    )

    prev_url = (
        f"""{domain}{reverse_lazy_with_query(
            "analytics:get_scores_by_community_id_analytics",
            args=[scorer_id],
            query_kwargs={"token": encode_cursor("prev", prev_id), "limit": limit},
        )}"""
        if has_prev_scores
        else None
    )

    response = CursorPaginatedScoreResponse(next=next_url, prev=prev_url, items=scores)

    return response
