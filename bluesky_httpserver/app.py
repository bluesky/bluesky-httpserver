import asyncio
import collections
import importlib
import logging
import os
import pprint
import re
import secrets
import urllib.parse
from functools import lru_cache, partial

from bluesky_queueserver.manager.comms import validate_zmq_key
from bluesky_queueserver_api.zmq.aio import REManagerAPI
from fastapi import APIRouter, FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi

from .authentication import Mode
from .console_output import CollectPublishedConsoleOutput
from .core import PatchedStreamingResponse
from .database.core import purge_expired
from .resources import SERVER_RESOURCES as SR
from .routers import core_api
from .settings import get_settings
from .utils import (
    API_KEY_COOKIE_NAME,
    CSRF_COOKIE_NAME,
    get_api_access_manager,
    get_authenticators,
    get_default_login_data,
    get_resource_access_manager,
    record_timing,
)

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

logging.basicConfig(level=logging.WARNING)
# logging.getLogger("bluesky_queueserver").setLevel("DEBUG")
logging.getLogger(__name__).setLevel("DEBUG")

SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}
SENSITIVE_COOKIES = {
    API_KEY_COOKIE_NAME,
}
CSRF_HEADER_NAME = "x-csrf"
CSRF_QUERY_PARAMETER = "csrf"


def custom_openapi(app):
    """
    The app's openapi method will be monkey-patched with this.

    This is the approach the documentation recommends.

    https://fastapi.tiangolo.com/advanced/extending-openapi/
    """
    from . import __version__

    if app.openapi_schema:
        return app.openapi_schema
    # Customize heading.
    openapi_schema = get_openapi(
        title="Bluesky HTTP Server",
        version=__version__,
        description="Control Experiments using Bluesky Queue Server",
        routes=app.routes,
    )
    # print(f"openapi_schema = {pprint.pformat(openapi_schema['components'])}")  ##
    # Insert refreshUrl.
    if "securitySchemes" in openapi_schema["components"]:  # False when calling /docs
        openapi_schema["components"]["securitySchemes"]["OAuth2PasswordBearer"]["flows"]["password"][
            "refreshUrl"
        ] = "token/refresh"
    app.openapi_schema = openapi_schema
    return app.openapi_schema


def add_router(app, *, module_and_router_name):
    """
    Include a router specified by module and router name represented as a string.

    Parameters
    ----------
    app: FastAPI
        Instantiated ``FastAPI`` object.
    module_and_router_name: str
        Name of the module and router object represented as a string, e.g. ``'some.module.router'``,
        where ``some.module`` is the module name and ``router`` is the name of the router object
        in the module.

    Raises
    ------
    ImportError
        Failed to include router, most likely because the module could not be imported or the router
        is not found.
    """
    try:
        components = module_and_router_name.split(".")
        if len(components) < 2:
            raise ValueError(
                f"Module name or router name is not found in {module_and_router_name!r}: "
                "expected format '<module-name>.<router-name>'"
            )
        module_name = ".".join(components[:-1])
        router_name = components[-1]
        mod = importlib.import_module(module_name)
        router = getattr(mod, router_name)
        app.include_router(router)
    except Exception as ex:
        raise ImportError(f"Failed to import router {module_and_router_name!r}: {ex}") from ex


def build_app(authentication=None, api_access=None, resource_access=None, server_settings=None):
    """
    Build application

    Parameters
    ----------
    authentication: dict, optional
        Dict of authentication configuration.
    server_settings: dict, optional
        Dict of other server configuration.
    """
    authentication = authentication or {}
    authentication_providers = authentication.get("providers", [])
    authenticators = {spec["provider"]: spec["authenticator"] for spec in authentication_providers}
    api_access = api_access or {}
    api_access_manager = api_access.get("manager_object", None)
    resource_access = resource_access or {}
    resource_access_manager = resource_access.get("manager_object", None)
    server_settings = server_settings or {}

    app = FastAPI()

    app.state.allow_origins = []

    # Include standard routers
    app.include_router(core_api.router)

    # Include custom routers
    router_names = []
    router_names_str = os.getenv("QSERVER_HTTP_CUSTOM_ROUTERS", None)
    if "custom_routers" in server_settings["server_configuration"]:
        router_names = server_settings["server_configuration"]["custom_routers"]
        logger.info("Custom routers are specified in the config file: %s", router_names)
    elif router_names_str:
        router_names = re.split(":|,", router_names_str)
        logger.info("Custom routers are specified in the environment variable: %s", router_names)

    if router_names:
        routers_already_included = set()
        for rn in router_names:
            if rn and (rn not in routers_already_included):
                logger.info("Including custom router '%s' ...", rn)
                routers_already_included.add(rn)
                add_router(app, module_and_router_name=rn)
        logger.info("All custom routers are included successfully.")

    from .authentication import (
        base_authentication_router,
        build_auth_code_route,
        build_handle_credentials_route,
        oauth2_scheme,
    )

    authentication_router = APIRouter()
    # This adds the universal routes like /session/refresh and /session/revoke.
    # Below we will add routes specific to our authentication providers.
    authentication_router.include_router(base_authentication_router)

    if authentication.get("providers", []):
        # For the OpenAPI schema, inject a OAuth2PasswordBearer URL.
        first_provider = authentication["providers"][0]["provider"]
        oauth2_scheme.model.flows.password.tokenUrl = f"/api/auth/provider/{first_provider}/token"
        # Authenticators provide Router(s) for their particular flow.
        # Collect them in the authentication_router.

        for spec in authentication["providers"]:
            provider = spec["provider"]
            authenticator = spec["authenticator"]
            mode = authenticator.mode
            if mode == Mode.password:
                authentication_router.post(f"/provider/{provider}/token")(
                    build_handle_credentials_route(authenticator, provider)
                )
            elif mode == Mode.external:
                authentication_router.get(f"/provider/{provider}/code")(
                    build_auth_code_route(authenticator, provider)
                )
                authentication_router.post(f"/provider/{provider}/code")(
                    build_auth_code_route(authenticator, provider)
                )
            else:
                raise ValueError(f"unknown authentication mode {mode}")
            for custom_router in getattr(authenticator, "include_routers", []):
                authentication_router.include_router(custom_router, prefix=f"/provider/{provider}")

    # And add this authentication_router itself to the app.
    app.include_router(authentication_router, prefix="/api/auth")

    @app.on_event("startup")
    async def startup_event():
        # Validate the single-user API key.
        settings = app.dependency_overrides[get_settings]()
        single_user_api_key = settings.single_user_api_key
        if single_user_api_key is not None:
            if not single_user_api_key.isalnum():
                raise ValueError(
                    "The API key must only contain alphanumeric characters. We enforce this because\n"
                    "pasting other characters into a URL, as in ?api_key=..., can result in\n"
                    "confusing behavior due to ambiguous encodings.\n\n"
                    "The API key can be as long as you like. Here are two ways to generate a valid\n"
                    "one:\n\n"
                    "# With openssl:\n"
                    "openssl rand -hex 32\n\n"
                    "# With Python:\n"
                    'python -c "import secrets; print(secrets.token_hex(32))"'
                )

        # Stash these to cancel this on shutdown.
        app.state.tasks = []
        # Authenticators can run tasks in the background.
        background_tasks = []
        for authenticator in authenticators:
            background_tasks.extend(getattr(authenticator, "background_tasks", []))
        background_tasks.extend(getattr(api_access_manager, "background_tasks", []))
        for task in background_tasks or []:
            asyncio_task = asyncio.create_task(task())
            app.state.tasks.append(asyncio_task)

        if settings.database_uri is not None:
            from sqlalchemy import create_engine

            # from sqlalchemy.orm import sessionmaker
            from .database import orm
            from .database.core import (  # make_admin_by_identity,
                REQUIRED_REVISION,
                UninitializedDatabase,
                check_database,
                initialize_database,
            )

            connect_args = {}
            if settings.database_uri.startswith("sqlite"):
                connect_args.update({"check_same_thread": False})

            engine = create_engine(settings.database_uri, connect_args=connect_args)
            redacted_url = engine.url._replace(password="[redacted]")
            try:
                check_database(engine)
            except UninitializedDatabase:
                # Create tables and stamp (alembic) revision.
                logger.info(
                    f"Database {redacted_url} is new. Creating tables and marking revision {REQUIRED_REVISION}."
                )
                initialize_database(engine)
                logger.info("Database initialized.")
            else:
                logger.info(f"Connected to existing database at {redacted_url}.")
            # SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
            # db = SessionLocal()
            # for admin in authentication.get("qserver_admins", []):
            #     logger.info(f"Ensuring that principal with identity {admin} has role 'admin'")
            #     make_admin_by_identity(
            #         db,
            #         identity_provider=admin["provider"],
            #         id=admin["id"],
            #     )

            async def purge_expired_sessions_and_api_keys():
                logger.info("Purging expired Sessions and API keys from the database.")
                while True:
                    await asyncio.get_running_loop().run_in_executor(None, purge_expired(engine, orm.Session))
                    await asyncio.get_running_loop().run_in_executor(None, purge_expired(engine, orm.APIKey))
                    await asyncio.sleep(600)

            app.state.tasks.append(asyncio.create_task(purge_expired_sessions_and_api_keys()))

        # TODO: implement nicer exit with error reporting in case of failure
        zmq_control_addr = os.getenv("QSERVER_ZMQ_CONTROL_ADDRESS", None)
        if zmq_control_addr is None:
            zmq_control_addr = os.getenv("QSERVER_ZMQ_ADDRESS_CONTROL", None)
            if zmq_control_addr is not None:
                logger.warning(
                    "Environment variable QSERVER_ZMQ_ADDRESS_CONTROL is deprecated: use environment variable "
                    "QSERVER_ZMQ_CONTROL_ADDRESS to pass address of 0MQ control socket to HTTP Server."
                )
        if zmq_control_addr is None:
            # Support for deprecated environment variable QSERVER_ZMQ_ADDRESS.
            # TODO: remove in one of the future versions
            zmq_control_addr = os.getenv("QSERVER_ZMQ_ADDRESS", None)
            if zmq_control_addr is not None:
                logger.warning(
                    "Environment variable QSERVER_ZMQ_ADDRESS is deprecated: use environment variable "
                    "QSERVER_ZMQ_CONTROL_ADDRESS to pass address of 0MQ control socket to HTTP Server."
                )

        zmq_info_addr = os.getenv("QSERVER_ZMQ_INFO_ADDRESS", None)
        if zmq_info_addr is None:
            # Support for deprecated environment variable QSERVER_ZMQ_ADDRESS.
            # TODO: remove in one of the future versions
            zmq_info_addr = os.getenv("QSERVER_ZMQ_ADDRESS_CONSOLE", None)
            if zmq_info_addr is not None:
                logger.warning(
                    "Environment variable QSERVER_ZMQ_ADDRESS_CONSOLE is deprecated: use environment variable "
                    "QSERVER_ZMQ_INFO_ADDRESS to pass address of 0MQ information socket to HTTP Server."
                )

        # Check if ZMQ setting were specified in config file. Overrid the parameters from EVs.
        zmq_control_addr = server_settings["qserver_zmq_configuration"].get("control_address", zmq_control_addr)
        zmq_info_addr = server_settings["qserver_zmq_configuration"].get("info_address", zmq_info_addr)

        # Read public key from the environment variable or config file.
        zmq_public_key = os.environ.get("QSERVER_ZMQ_PUBLIC_KEY", None)
        zmq_public_key = zmq_public_key if zmq_public_key else None  # Case of ""
        zmq_public_key = server_settings["qserver_zmq_configuration"].get("public_key", zmq_public_key)
        if zmq_public_key is not None:
            try:
                validate_zmq_key(zmq_public_key)
            except Exception as ex:
                raise ValueError(f"ZMQ public key is improperly formatted: {ex}")

        logger.info(
            f"Connecting to RE Manager: \nControl 0MQ socket address: {zmq_control_addr}\n"
            f"Information 0MQ socket address: {zmq_info_addr}"
        )

        RM = REManagerAPI(
            zmq_control_addr=zmq_control_addr,
            zmq_info_addr=zmq_info_addr,
            zmq_public_key=zmq_public_key,
            request_fail_exceptions=False,
            status_expiration_period=0.4,  # Make it smaller than default
            console_monitor_max_lines=2000,
        )
        SR.set_RM(RM)

        login_data = get_default_login_data()
        SR.RM._user = login_data["user"]
        SR.RM._user_group = login_data["user_group"]

        SR.set_console_output_loader(CollectPublishedConsoleOutput(rm_ref=RM))
        SR.console_output_loader.start()

        # Import module with custom code
        module_names_str = os.getenv("QSERVER_CUSTOM_MODULES", None)
        if (module_names_str is None) and (os.getenv("QSERVER_CUSTOM_MODULE", None) is not None):
            logger.warning(
                "Environment variable QSERVER_CUSTOM_MODULE is deprecated and will be removed. "
                "Use the environment variable QSERVER_CUSTOM_MODULES, which accepts a string with "
                "comma or colon-separated module names."
            )
        module_names_str = module_names_str or os.getenv("QSERVER_CUSTOM_MODULE", None)

        module_names = []
        if "custom_modules" in server_settings["server_configuration"]:
            module_names = server_settings["server_configuration"]["custom_modules"]
            logger.info("Custom modules from config file: %s", pprint.pformat(module_names))
        elif module_names_str:
            module_names = re.split(":|,", module_names_str)
            logger.info("Custom modules from environment variable: %s", pprint.pformat(module_names))

        if module_names:
            # Import all listed custom modules
            custom_code_modules = []
            for name in module_names:
                try:
                    logger.info("Importing custom module '%s' ...", name)
                    custom_code_modules.append(importlib.import_module(name.replace("-", "_")))
                    logger.info("Module '%s' was imported successfully.", name)
                except Exception as ex:
                    logger.error("Failed to import custom instrument module '%s': %s", name, ex)
            SR.set_custom_code_modules(custom_code_modules)
        else:
            SR.set_custom_code_modules([])

        # The following message is used in unit tests to detect when HTTP server is started.
        #   Unit tests need to be modified if this message is modified.
        logger.info("Bluesky HTTP Server started successfully")

    @app.on_event("shutdown")
    async def shutdown_event():
        await SR.RM.close()
        await SR.console_output_loader.stop()

    @lru_cache(1)
    def override_get_authenticators():
        return authenticators

    @lru_cache(1)
    def override_get_api_access_manager():
        return api_access_manager

    @lru_cache(1)
    def override_get_resource_access_manager():
        return resource_access_manager

    @lru_cache(1)
    def override_get_settings():
        settings = get_settings()
        setattr(settings, "authentication_provider_names", [_["provider"] for _ in authentication_providers])
        for item in [
            "allow_anonymous_access",
            "secret_keys",
            "single_user_api_key",
            "access_token_max_age",
            "refresh_token_max_age",
            "session_max_age",
        ]:
            if authentication.get(item) is not None:
                setattr(settings, item, authentication[item])
        if authentication.get("single_user_api_key") is not None:
            setattr(settings, "single_user_api_key_generated", False)
        for item in ["allow_origins", "response_bytesize_limit"]:
            if server_settings.get(item) is not None:
                setattr(settings, item, server_settings[item])
        database = server_settings.get("database", {})
        if database.get("uri"):
            settings.database_uri = database["uri"]
        if database.get("pool_size"):
            settings.database_pool_size = database["pool_size"]
        if database.get("pool_pre_ping"):
            settings.database_pool_pre_ping = database["pool_pre_ping"]
        object_cache_available_bytes = server_settings.get("object_cache", {}).get("available_bytes")
        if object_cache_available_bytes is not None:
            setattr(settings, "object_cache_available_bytes", object_cache_available_bytes)
        if authentication.get("providers"):
            # If we support authentication providers, we need a database, so if one is
            # not set, use a SQLite database in the current working directory.
            settings.database_uri = settings.database_uri or "sqlite:///./bluesky_httpserver.sqlite"
        return settings

    @app.middleware("http")
    async def capture_metrics(request: Request, call_next):
        """
        Place metrics in Server-Timing header, in accordance with HTTP spec.
        """
        # https://developer.mozilla.org/en-US/docs/Web/HTTP/Headers/Server-Timing
        # https://w3c.github.io/server-timing/#the-server-timing-header-field
        # This information seems safe to share because the user can easily
        # estimate it based on request/response time, but if we add more detailed
        # information here we should keep in mind security concerns and perhaps
        # only include this for certain users.
        # Initialize a dict that routes and dependencies can stash metrics in.
        metrics = collections.defaultdict(lambda: collections.defaultdict(lambda: 0))
        request.state.metrics = metrics
        # Record the overall application time.
        with record_timing(metrics, "app"):
            response = await call_next(request)
        # Server-Timing specifies times should be in milliseconds.
        # Prometheus specifies times should be in seconds.
        # Therefore, we store as seconds and convert to ms for Server-Timing here.
        # That is what the factor of 1000 below is doing.
        response.headers["Server-Timing"] = ", ".join(
            f"{key};"
            + ";".join(
                (f"{metric}={value * 1000:.1f}" if metric == "dur" else f"{metric}={value:.1f}")
                for metric, value in metrics_.items()
            )
            for key, metrics_ in metrics.items()
        )
        response.__class__ = PatchedStreamingResponse  # tolerate memoryview
        return response

    @app.middleware("http")
    async def double_submit_cookie_csrf_protection(request: Request, call_next):
        # https://cheatsheetseries.owasp.org/cheatsheets/Cross-Site_Request_Forgery_Prevention_Cheat_Sheet.html#double-submit-cookie
        csrf_cookie = request.cookies.get(CSRF_COOKIE_NAME)
        if (request.method not in SAFE_METHODS) and set(request.cookies).intersection(SENSITIVE_COOKIES):
            if not csrf_cookie:
                return Response(status_code=403, content="Expected tiled_csrf_token cookie")
            # Get the token from the Header or (if not there) the query parameter.
            csrf_token = request.headers.get(CSRF_HEADER_NAME)
            if csrf_token is None:
                parsed_query = urllib.parse.parse_qs(request.url.query)
                csrf_token = parsed_query.get(CSRF_QUERY_PARAMETER)
            if not csrf_token:
                return Response(
                    status_code=403,
                    content=f"Expected {CSRF_QUERY_PARAMETER} query parameter or {CSRF_HEADER_NAME} header",
                )
            # Securely compare the token with the cookie.
            if not secrets.compare_digest(csrf_token, csrf_cookie):
                return Response(status_code=403, content="Double-submit CSRF tokens do not match")

        response = await call_next(request)
        response.__class__ = PatchedStreamingResponse  # tolerate memoryview
        if not csrf_cookie:
            response.set_cookie(
                key=CSRF_COOKIE_NAME,
                value=secrets.token_urlsafe(32),
                httponly=True,
                samesite="lax",
            )
        return response

    @app.middleware("http")
    async def set_cookies(request: Request, call_next):
        "This enables dependencies to inject cookies that they want to be set."
        # Create some Request state, to be (possibly) populated by dependencies.
        request.state.cookies_to_set = []
        response = await call_next(request)
        response.__class__ = PatchedStreamingResponse  # tolerate memoryview
        for params in request.state.cookies_to_set:
            params.setdefault("httponly", True)
            params.setdefault("samesite", "lax")
            response.set_cookie(**params)
        return response

    app.openapi = partial(custom_openapi, app)
    app.dependency_overrides[get_authenticators] = override_get_authenticators
    app.dependency_overrides[get_api_access_manager] = override_get_api_access_manager
    app.dependency_overrides[get_resource_access_manager] = override_get_resource_access_manager
    app.dependency_overrides[get_settings] = override_get_settings

    def add_custom_middleware():
        settings = app.dependency_overrides[get_settings]()
        app.state.allow_origins.extend(settings.allow_origins)
        app.add_middleware(
            CORSMiddleware,
            allow_origins=app.state.allow_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    add_custom_middleware()

    return app
