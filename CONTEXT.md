# Domain glossary

## User

A person represented inside OpenClaw Manager. A user can own multiple instances
and can be linked to multiple external identities. Deleting an instance does
not delete its owner.

## User identity

A login identity issued by one authentication provider. It is uniquely
identified by `(provider, subject)` and maps to exactly one User. Usernames,
email addresses, and display names are not identity keys.

Binding an identity does not enable it. OpenClaw Manager activates exactly one
authentication provider at a time; every accepted identity still resolves to
the same internal User.

## Instance

One managed deployment of a product such as OpenClaw, Hermes, or EvoScientist.
An instance has its own stable public ID, runtime identifier, data path,
credentials, endpoints, and lifecycle status.

## Runtime identifier

The platform-controlled identifier used by the runtime, such as a Docker
container name or Kubernetes workload name. Clients cannot supply it to
lifecycle operations.

## Legacy user ID

The identifier used by the original one-user/one-instance model. It remains
only as a compatibility lookup key for migrated instances.

## Endpoint

A published or internal route to an instance. A legacy host port and a future
HTTPS subdomain are different endpoint types for the same Instance.
