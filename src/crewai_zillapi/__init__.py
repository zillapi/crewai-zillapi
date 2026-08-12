"""crewai-zillapi: CrewAI tools for Zillapi (zillapi.com)."""

from .zillapi_tools import (
    ZillapiAccountToolSchema,
    ZillapiJobToolSchema,
    ZillapiListingSearchToolSchema,
    ZillapiPropertyByAddressToolSchema,
    ZillapiPropertyByUrlToolSchema,
    ZillapiPropertyByZpidToolSchema,
    ZillapiPropertyDetailToolSchema,
)

try:
    from .zillapi_tools import (
        ZillapiAccountTool,
        ZillapiJobTool,
        ZillapiListingSearchTool,
        ZillapiPropertyByAddressTool,
        ZillapiPropertyByUrlTool,
        ZillapiPropertyByZpidTool,
        ZillapiPropertyDetailTool,
    )
except ImportError:  # pragma: no cover - crewai not installed
    pass

__version__ = "0.1.0"

__all__ = [
    "ZillapiPropertyByAddressTool",
    "ZillapiPropertyByZpidTool",
    "ZillapiPropertyDetailTool",
    "ZillapiPropertyByUrlTool",
    "ZillapiListingSearchTool",
    "ZillapiJobTool",
    "ZillapiAccountTool",
    "ZillapiPropertyByAddressToolSchema",
    "ZillapiPropertyByZpidToolSchema",
    "ZillapiPropertyDetailToolSchema",
    "ZillapiPropertyByUrlToolSchema",
    "ZillapiListingSearchToolSchema",
    "ZillapiJobToolSchema",
    "ZillapiAccountToolSchema",
    "__version__",
]
