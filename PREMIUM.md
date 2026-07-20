# Premium Features Activation Guide

## Quick Setup

### Option 1: Environment Variable (.env)

Add to your `.env` file:

```bash
LITELLM_PREMIUM_USER=True
```

Then in `litellm/proxy/auth/litellm_license.py`, modify `is_premium()`:

```python
if os.getenv("LITELLM_PREMIUM_USER", "").lower() == "true":
    return True
```

### Option 2: config.yaml

In your `config.yaml`:

```yaml
litellm_settings:
  premium_user: true
```

### Option 3: Direct Variable in proxy_server.py

In `litellm/proxy/proxy_server.py`, add at the top:

```python
premium_user = True
```

### Option 4: Python Code (Runtime)

Before LiteLLM starts:

```python
import litellm.proxy.proxy_server
litellm.proxy.proxy_server.premium_user = True
```

## Verification

Check if premium is active:

```python
from litellm.proxy.auth.litellm_license import LicenseCheck
license = LicenseCheck()
print(license.is_premium())  # Should return True
```
