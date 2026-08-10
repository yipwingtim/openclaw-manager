PRODUCT_CAPABILITIES = {
    "openclaw": frozenset(
        {
            "status", "logs", "start", "stop", "restart", "create",
            "batch_create", "delete", "restore", "purge_deleted", "update_version",
            "batch_set_model_provider", "basic_auth", "skill_install", "dashboard", "access",
            "device_pairing", "file_upload", "file_download", "file_delete",
        }
    ),
    "evoscientist": frozenset(
        {"access", "status", "logs", "start", "stop", "restart", "create",
         "delete", "restore", "purge_deleted", "update_version", "cleanup_failed",
         "batch_set_model_provider"}
    ),
    "hermes": frozenset(
        {
            "access", "status", "logs", "start", "stop", "restart", "create",
            "delete", "restore", "purge_deleted", "update_version", "batch_set_model_provider",
        }
    ),
}

INSTANCE_AUTH_CONTRACTS = {
    "openclaw": {
        "edge_authorization": "uis",
        "product_auth": "token",
        "identity_header": None,
    },
    "hermes": {
        "edge_authorization": "uis",
        "product_auth": "session",
        "identity_header": None,
    },
    "evoscientist": {
        "edge_authorization": "uis",
        "product_auth": "none",
        "identity_header": None,
    },
}

EXECUTION_ACTION_CAPABILITIES = {
    "instance.create": "create",
    "instance.start": "start",
    "instance.stop": "stop",
    "instance.restart": "restart",
    "instance.set_basic_auth": "basic_auth",
    "instance.update_version": "update_version",
    "instance.install_skill": "skill_install",
    "instance.set_model_provider": "batch_set_model_provider",
    "instance.refresh_devices": "device_pairing",
    "instance.approve_latest_device": "device_pairing",
    "instance.delete": "delete",
    "instance.restore": "restore",
    "instance.purge_deleted": "purge_deleted",
    "instance.cleanup_failed": "cleanup_failed",
    "instance.wechat_bind": "device_pairing",
}


def product_capabilities(product):
    return PRODUCT_CAPABILITIES.get(product, frozenset())


def product_supports(product, capability):
    return capability in product_capabilities(product)


def product_auth_contract(product):
    contract = INSTANCE_AUTH_CONTRACTS.get(product)
    if contract is None:
        return None
    return dict(contract)


def execution_action_capability(action):
    return EXECUTION_ACTION_CAPABILITIES.get(action)
