"""Compatibility re-export of schema generator and validator from atobar_contracts."""

from atobar_contracts.schema import (
    SCHEMAS_DIR,
    check_schemas,
    generate_schemas,
    main,
    write_schemas,
)

__all__ = [
    "SCHEMAS_DIR",
    "check_schemas",
    "generate_schemas",
    "main",
    "write_schemas",
]

if __name__ == "__main__":
    main()
