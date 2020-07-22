from typing import Optional

import opentracing
import opentracing.tags
from django.conf import settings
from django.contrib.auth import authenticate
from django.contrib.auth.models import AnonymousUser
from django.utils.functional import SimpleLazyObject
from graphql import ResolveInfo
from graphql.error import GraphQLError

from ..app.models import App
from ..channel.exceptions import ChannelSlugNotPassedException, NoChannelException
from ..channel.utils import get_default_channel_slug_if_available
from ..core.exceptions import ReadOnlyException
from ..core.tracing import should_trace
from .views import API_PATH, GraphQLView


def get_user(request):
    if not hasattr(request, "_cached_user"):
        request._cached_user = authenticate(request=request)
    return request._cached_user


class JWTMiddleware:
    def resolve(self, next, root, info, **kwargs):
        request = info.context

        def user():
            return get_user(request) or AnonymousUser()

        request.user = SimpleLazyObject(lambda: user())
        return next(root, info, **kwargs)


class ChannelMiddleware:
    def resolve(self, next, root, info, **kwargs):
        request = info.context
        request_input = kwargs.get("input", {})
        channel = kwargs.get("channel")
        if isinstance(request_input, dict) and request_input:
            channel_slug = request_input.get("channel_slug")
        elif channel is not None:
            channel_slug = channel
        else:
            channel_slug = None
        if (
            hasattr(request, "channel_slug")
            and isinstance(request.channel_slug, SimpleLazyObject)
            and channel_slug
        ):
            request.channel_slug = channel_slug
        if not hasattr(request, "channel_slug"):

            def get_channel_slug(channel_slug):
                if not channel_slug:
                    try:
                        channel_slug = get_default_channel_slug_if_available()
                    except ChannelSlugNotPassedException:
                        raise GraphQLError("Argument 'channel` not passed.")
                    except NoChannelException:
                        return None
                return channel_slug

            request.channel_slug = SimpleLazyObject(
                lambda: get_channel_slug(channel_slug)
            )
        return next(root, info, **kwargs)


class OpentracingGrapheneMiddleware:
    @staticmethod
    def resolve(next_, root, info: ResolveInfo, **kwargs):
        if not should_trace(info):
            return next_(root, info, **kwargs)
        operation = f"{info.parent_type.name}.{info.field_name}"
        with opentracing.global_tracer().start_active_span(operation) as scope:
            span = scope.span
            span.set_tag(opentracing.tags.COMPONENT, "graphql")
            span.set_tag("graphql.parent_type", info.parent_type.name)
            span.set_tag("graphql.field_name", info.field_name)
            return next_(root, info, **kwargs)


def get_app(auth_token) -> Optional[App]:
    qs = App.objects.filter(tokens__auth_token=auth_token, is_active=True)
    return qs.first()


def app_middleware(next, root, info, **kwargs):

    app_auth_header = "HTTP_AUTHORIZATION"
    prefix = "bearer"
    request = info.context

    if request.path == API_PATH:
        if not hasattr(request, "app"):
            request.app = None
            auth = request.META.get(app_auth_header, "").split()
            if len(auth) == 2:
                auth_prefix, auth_token = auth
                if auth_prefix.lower() == prefix:
                    request.app = SimpleLazyObject(lambda: get_app(auth_token))
    return next(root, info, **kwargs)


class ReadOnlyMiddleware:
    ALLOWED_MUTATIONS = [
        "checkoutAddPromoCode",
        "checkoutBillingAddressUpdate",
        "checkoutComplete",
        "checkoutCreate",
        "checkoutCustomerAttach",
        "checkoutCustomerDetach",
        "checkoutEmailUpdate",
        "checkoutLineDelete",
        "checkoutLinesAdd",
        "checkoutLinesUpdate",
        "checkoutRemovePromoCode",
        "checkoutPaymentCreate",
        "checkoutShippingAddressUpdate",
        "checkoutShippingMethodUpdate",
        "tokenCreate",
        "tokenVerify",
    ]

    @staticmethod
    def resolve(next_, root, info, **kwargs):
        operation = info.operation.operation
        if operation != "mutation":
            return next_(root, info, **kwargs)

        # Bypass users authenticated with ROOT_EMAIL
        request = info.context
        user = getattr(request, "user", None)
        if user and not user.is_anonymous:
            user_email = user.email
            root_email = getattr(settings, "ROOT_EMAIL", None)
            if root_email and user_email == root_email:
                return next_(root, info, **kwargs)

        for selection in info.operation.selection_set.selections:
            selection_name = str(selection.name.value)
            blocked = selection_name not in ReadOnlyMiddleware.ALLOWED_MUTATIONS
            if blocked:
                raise ReadOnlyException(
                    "Be aware admin pirate! API runs in read-only mode!"
                )
        return next_(root, info, **kwargs)


def process_view(self, request, view_func, *args):
    if hasattr(view_func, "view_class") and issubclass(
        view_func.view_class, GraphQLView
    ):
        request._graphql_view = True


if settings.ENABLE_DEBUG_TOOLBAR:
    import warnings

    try:
        from graphiql_debug_toolbar.middleware import DebugToolbarMiddleware
    except ImportError:
        warnings.warn("The graphiql debug toolbar was not installed.")
    else:
        DebugToolbarMiddleware.process_view = process_view
