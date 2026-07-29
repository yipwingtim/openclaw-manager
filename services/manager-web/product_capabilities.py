PRODUCT_CAPABILITIES = {
    "openclaw": frozenset(
        {
            "status", "logs", "start", "stop", "restart", "create",
            "batch_create", "delete", "restore", "update_version",
            "batch_set_model_provider", "basic_auth", "skill_install", "dashboard", "access",
            "device_pairing", "file_upload", "file_download", "file_delete",
        }
    ),
    "evoscientist": frozenset(
        {"access", "status", "logs", "start", "stop", "restart"}
    ),
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
    "instance.wechat_bind": "device_pairing",
}


def product_capabilities(product):
    return PRODUCT_CAPABILITIES.get(product, frozenset())


def product_supports(product, capability):
    return capability in product_capabilities(product)


def execution_action_capability(action):
    return EXECUTION_ACTION_CAPABILITIES.get(action)
