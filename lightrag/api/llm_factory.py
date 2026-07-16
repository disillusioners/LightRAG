"""
LightRAG API role-LLM factory helpers.

This module is extracted from :mod:`lightrag.api.lightrag_server` so that the
per-workspace :class:`WorkspaceManager` (and any other non-FastAPI code path
that needs to build a fully-initialized :class:`lightrag.LightRAG` instance)
can construct role-specific LLM functions without depending on the FastAPI
``create_app`` closure.

The two factories plus their shared ``resolve_role_llm_settings`` helper used
to be defined as nested functions inside ``create_app``. That made them
implicitly capture the enclosing ``args`` (the parsed CLI / API config) and
``llm_timeout`` local. Once they need to be importable from another module,
those closure values are passed in explicitly as parameters. The behavior is
unchanged — only the resolution mechanism switches from a closure cell to a
plain function argument.

Public entry points:

* :func:`resolve_role_llm_settings` — resolve role-specific config dict.
* :func:`create_role_llm_func` — build the raw async LLM function for a role.
* :func:`create_role_llm_model_kwargs` — role-specific kwargs for runtime
  wrapper injection (intentionally empty here; see the function docstring).
* :func:`register_role_llm_builder` — register the per-role LLM builder on a
  :class:`LightRAG` instance (must be called on every instance, including
  per-workspace instances created by ``WorkspaceManager``).
"""

from __future__ import annotations

import os
from typing import Any

from lightrag import LightRAG


def resolve_role_llm_settings(
    role: str,
    args: Any,
    llm_timeout: int,
    override_meta: dict | None = None,
) -> dict[str, Any]:
    """Resolve role-specific LLM configuration from ``args`` and optional overrides.

    Mirrors the logic that used to live as a nested function inside
    ``create_app``. ``args`` and ``llm_timeout`` are passed in explicitly so the
    function can be reused outside the FastAPI server module.
    """
    attr = role.lower()
    override_meta = override_meta or {}

    role_binding = (
        override_meta.get("binding")
        or getattr(args, f"{attr}_llm_binding", None)
        or args.llm_binding
    )
    role_model = (
        override_meta.get("model")
        or getattr(args, f"{attr}_llm_model", None)
        or args.llm_model
    )
    role_host = (
        override_meta.get("host")
        or getattr(args, f"{attr}_llm_binding_host", None)
        or args.llm_binding_host
    )
    explicit_role_apikey = override_meta.get("api_key") or getattr(
        args, f"{attr}_llm_binding_api_key", None
    )
    if role_binding == "bedrock":
        if explicit_role_apikey:
            raise ValueError(
                f"Bedrock role '{role}' does not support role-specific "
                "LLM_BINDING_API_KEY; use role-specific SigV4 AWS_* "
                "variables or process-level AWS_BEARER_TOKEN_BEDROCK."
            )
        role_apikey = None
    else:
        role_apikey = explicit_role_apikey or args.llm_binding_api_key
    role_timeout = (
        override_meta.get("timeout")
        or getattr(args, f"{attr}_llm_timeout", None)
        or llm_timeout
    )
    role_max_async = override_meta.get("max_async")
    if role_max_async is None:
        role_max_async = getattr(args, f"{attr}_llm_max_async", None)
    is_cross_provider = role_binding != args.llm_binding

    role_provider_options = override_meta.get("provider_options")
    if role_provider_options is None:
        if role_binding in ["openai", "azure_openai"]:
            from lightrag.llm.binding_options import OpenAILLMOptions

            role_provider_options = OpenAILLMOptions.options_dict_for_role(
                args, role, is_cross_provider
            )
        elif role_binding == "gemini":
            from lightrag.llm.binding_options import GeminiLLMOptions

            role_provider_options = GeminiLLMOptions.options_dict_for_role(
                args, role, is_cross_provider
            )
        elif role_binding in ["lollms", "ollama"]:
            from lightrag.llm.binding_options import OllamaLLMOptions

            role_provider_options = OllamaLLMOptions.options_dict_for_role(
                args, role, is_cross_provider
            )
        elif role_binding == "bedrock":
            from lightrag.llm.binding_options import BedrockLLMOptions

            role_provider_options = BedrockLLMOptions.options_dict_for_role(
                args, role, is_cross_provider
            )
        else:
            role_provider_options = {}

    bedrock_aws_options = {}
    if role_binding == "bedrock":
        override_bedrock_aws_options = override_meta.get("bedrock_aws_options", {})
        bedrock_aws_options = {
            "aws_region": override_meta.get("aws_region")
            or override_bedrock_aws_options.get("aws_region")
            or getattr(args, f"{attr}_aws_region", None)
            or getattr(args, "aws_region", None),
            "aws_access_key_id": override_meta.get("aws_access_key_id")
            or override_bedrock_aws_options.get("aws_access_key_id")
            or getattr(args, f"{attr}_aws_access_key_id", None)
            or getattr(args, "aws_access_key_id", None),
            "aws_secret_access_key": override_meta.get("aws_secret_access_key")
            or override_bedrock_aws_options.get("aws_secret_access_key")
            or getattr(args, f"{attr}_aws_secret_access_key", None)
            or getattr(args, "aws_secret_access_key", None),
            "aws_session_token": override_meta.get("aws_session_token")
            or override_bedrock_aws_options.get("aws_session_token")
            or getattr(args, f"{attr}_aws_session_token", None)
            or getattr(args, "aws_session_token", None),
        }

    return {
        "binding": role_binding,
        "model": role_model,
        "host": role_host,
        "api_key": role_apikey,
        "timeout": role_timeout,
        "max_async": role_max_async,
        "provider_options": role_provider_options,
        "is_cross_provider": is_cross_provider,
        "bedrock_aws_options": bedrock_aws_options,
    }


def create_role_llm_func(
    role: str, args: Any, llm_timeout: int, override_meta: dict | None = None
):
    """Create an independent raw LLM function for a role.

    The returned coroutine function closes over the resolved role settings and
    delegates to the provider-specific ``*_complete_if_cache`` implementation.
    Provider modules are imported lazily so missing optional dependencies only
    surface when that binding is actually requested.
    """
    settings = resolve_role_llm_settings(role, args, llm_timeout, override_meta)
    role_binding = settings["binding"]
    role_model = settings["model"]
    role_host = settings["host"]
    role_apikey = settings["api_key"]
    role_timeout = settings["timeout"]
    role_provider_options = settings["provider_options"]
    bedrock_aws_options = settings["bedrock_aws_options"]

    try:
        if role_binding == "ollama":
            from lightrag.llm.ollama import _ollama_model_if_cache

            async def role_ollama_complete(
                prompt,
                system_prompt=None,
                history_messages=None,
                enable_cot: bool = False,
                **kwargs,
            ):
                # response_format and legacy extraction booleans flow
                # through kwargs to _ollama_model_if_cache, which handles
                # the deprecation shim and emits a single warning.
                if history_messages is None:
                    history_messages = []
                if role_provider_options:
                    kwargs.setdefault("options", dict(role_provider_options))
                return await _ollama_model_if_cache(
                    role_model,
                    prompt,
                    system_prompt=system_prompt,
                    history_messages=history_messages,
                    enable_cot=enable_cot,
                    host=role_host,
                    timeout=role_timeout,
                    api_key=role_apikey,
                    **kwargs,
                )

            return role_ollama_complete
        if role_binding == "lollms":
            from lightrag.llm.lollms import lollms_model_if_cache

            async def role_lollms_complete(
                prompt,
                system_prompt=None,
                history_messages=None,
                enable_cot: bool = False,
                **kwargs,
            ):
                # response_format and legacy extraction booleans flow
                # through kwargs to lollms_model_if_cache, which drops
                # them and emits deprecation warnings when booleans are set.
                if history_messages is None:
                    history_messages = []
                if role_provider_options:
                    kwargs = {**role_provider_options, **kwargs}
                return await lollms_model_if_cache(
                    role_model,
                    prompt,
                    system_prompt=system_prompt,
                    history_messages=history_messages,
                    enable_cot=enable_cot,
                    base_url=role_host,
                    api_key=role_apikey,
                    timeout=role_timeout,
                    **kwargs,
                )

            return role_lollms_complete
        if role_binding == "bedrock":
            from lightrag.llm.bedrock import bedrock_complete_if_cache

            async def role_bedrock_complete(
                prompt,
                system_prompt=None,
                history_messages=None,
                **kwargs,
            ) -> str:
                if history_messages is None:
                    history_messages = []
                if role_provider_options:
                    kwargs = {**role_provider_options, **kwargs}
                return await bedrock_complete_if_cache(
                    role_model,
                    prompt,
                    system_prompt=system_prompt,
                    history_messages=history_messages,
                    endpoint_url=role_host,
                    **bedrock_aws_options,
                    **kwargs,
                )

            return role_bedrock_complete
        if role_binding == "azure_openai":
            from lightrag.llm.azure_openai import azure_openai_complete_if_cache

            async def role_azure_openai_complete(
                prompt,
                system_prompt=None,
                history_messages=None,
                **kwargs,
            ) -> str:
                if history_messages is None:
                    history_messages = []
                kwargs["timeout"] = role_timeout
                if role_provider_options:
                    kwargs.update(role_provider_options)
                return await azure_openai_complete_if_cache(
                    role_model,
                    prompt,
                    system_prompt=system_prompt,
                    history_messages=history_messages,
                    base_url=role_host,
                    api_key=role_apikey or os.getenv("AZURE_OPENAI_API_KEY"),
                    api_version=os.getenv(
                        "AZURE_OPENAI_API_VERSION", "2024-08-01-preview"
                    ),
                    **kwargs,
                )

            return role_azure_openai_complete
        if role_binding == "gemini":
            from lightrag.llm.gemini import gemini_complete_if_cache

            async def role_gemini_complete(
                prompt,
                system_prompt=None,
                history_messages=None,
                **kwargs,
            ) -> str:
                if history_messages is None:
                    history_messages = []
                kwargs["timeout"] = role_timeout
                if role_provider_options and "generation_config" not in kwargs:
                    kwargs["generation_config"] = dict(role_provider_options)
                return await gemini_complete_if_cache(
                    role_model,
                    prompt,
                    system_prompt=system_prompt,
                    history_messages=history_messages,
                    api_key=role_apikey,
                    base_url=role_host,
                    **kwargs,
                )

            return role_gemini_complete

        from lightrag.llm.openai import openai_complete_if_cache

        async def role_openai_complete(
            prompt,
            system_prompt=None,
            history_messages=None,
            **kwargs,
        ) -> str:
            if history_messages is None:
                history_messages = []
            kwargs["timeout"] = role_timeout
            if role_provider_options:
                kwargs.update(role_provider_options)
            return await openai_complete_if_cache(
                role_model,
                prompt,
                system_prompt=system_prompt,
                history_messages=history_messages,
                base_url=role_host,
                api_key=role_apikey,
                **kwargs,
            )

        return role_openai_complete
    except ImportError as e:
        raise Exception(f"Failed to create LLM for role '{role}': {e}")


def create_role_llm_model_kwargs(
    role: str, override_meta: dict | None = None
) -> dict[str, Any] | None:
    """Create role-specific kwargs for runtime wrapper injection.

    Role functions built above already encapsulate provider host/model/api_key/options,
    so we intentionally return an empty dict here to prevent base kwargs inheritance
    from polluting cross-provider role calls.
    """
    _ = role
    _ = override_meta
    return {}


def register_role_llm_builder(rag: LightRAG, args: Any, llm_timeout: int) -> None:
    """Register the role-LLM builder on a :class:`LightRAG` instance.

    This must be called on EVERY new :class:`LightRAG` instance, including the
    per-workspace instances that ``WorkspaceManager`` builds at runtime.
    Without this registration the role system falls back to the base LLM for
    non-default roles, which silently breaks cross-provider role routing.

    ``args`` and ``llm_timeout`` are passed through to
    :func:`create_role_llm_func` so role functions resolved lazily by
    ``LightRAG`` see the same configuration the instance was initialized
    with.
    """
    rag.register_role_llm_builder(
        lambda role, meta: (
            create_role_llm_func(role, args, llm_timeout, meta),
            create_role_llm_model_kwargs(role, meta),
        )
    )
