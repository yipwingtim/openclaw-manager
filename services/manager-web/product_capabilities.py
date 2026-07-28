PRODUCT_CAPABILITIES = {
    "openclaw": frozenset(
        {
            "status", "logs", "start", "stop", "restart", "create",
            "batch_create", "delete", "restore", "update_version",
            "batch_set_model_provider", "basic_auth", "dashboard", "access",
            "device_pairing", "file_upload", "file_download", "file_delete",
        }
    ),
    "evoscientist": frozenset(
        {"access", "status", "logs", "start", "stop", "restart"}
    ),
}

EXECUTION_ACTION_CAPABILITIES = {
    "instance.start": "start",
    "instance.stop": "stop",
    "instance.restart": "restart",
    "instance.wechat_bind": "device_pairing",
}


def product_capabilities(product):
    return PRODUCT_CAPABILITIES.get(product, frozenset())


def product_supports(product, capability):
    return capability in product_capabilities(product)


def execution_action_capability(action):
    return EXECUTION_ACTION_CAPABILITIES.get(action)
