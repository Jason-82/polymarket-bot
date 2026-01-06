# Security Guidelines

This document outlines security best practices for running the Polymarket trading bot.

## Secrets Management

### Never Commit Secrets

The following should **NEVER** be committed to version control:

- `.env` files
- Private keys
- API credentials
- Wallet seed phrases
- Any file containing sensitive data

Use `.gitignore` to exclude these files (already configured).

### Environment Variables

Secrets should be provided via environment variables or `.env` file:

```bash
# Copy template
cp .env.example .env

# Edit with your values
# NEVER share or commit this file
```

### Where Secrets Are Used

| Secret | Used For | Notes |
|--------|----------|-------|
| `PRIVATE_KEY` | Order signing (LIVE mode) | Required for trading |
| `TELEGRAM_BOT_TOKEN` | Alert notifications | Optional |
| `TELEGRAM_CHAT_ID` | Alert notifications | Optional |
| `SLACK_WEBHOOK_URL` | Alert notifications | Optional |

### Secrets in Logs

The bot is designed to **never** log secrets. However:

- Review logs before sharing them
- Be cautious with DEBUG log level
- Never post logs publicly without redacting addresses

## Wallet Security

### Dedicated Trading Wallet

**Always use a dedicated wallet for trading:**

1. Create a new wallet just for this bot
2. Only fund it with what you're willing to lose
3. Do not use your main wallet or a wallet with significant holdings
4. Consider this wallet as "hot" and at-risk

### Fund Management

- Start with minimal funds (e.g., $100)
- Only increase after extensive testing
- Regularly withdraw profits to a cold wallet
- Set exchange limits to match your risk tolerance

### Private Key Handling

If you must store a private key:

1. Use environment variables, not files
2. Consider using a hardware wallet with limited permissions (advanced)
3. Rotate keys periodically
4. Never share or email private keys

## Rotating Credentials

### Rotating Private Keys

To rotate your trading wallet:

1. Create a new wallet
2. Transfer remaining funds from old wallet
3. Update `PRIVATE_KEY` in `.env`
4. Restart the bot
5. Verify the old wallet is empty

### Rotating API Credentials

If API credentials are compromised:

1. Revoke old credentials immediately (if supported by exchange)
2. Generate new credentials
3. Update environment variables
4. Restart the bot
5. Monitor for unauthorized activity

## Dependency Security

### Pinned Versions

Dependencies in `requirements.txt` are pinned to specific versions. This:

- Prevents supply chain attacks
- Ensures reproducible builds
- Avoids unexpected breaking changes

### Security Scanning

Periodically scan dependencies for vulnerabilities:

```bash
# Install pip-audit
pip install pip-audit

# Scan dependencies
pip-audit

# Update vulnerable packages
pip install --upgrade <package-name>
```

### Third-Party Packages

The bot uses these external packages:

- `httpx` - HTTP client (well-maintained, async-native)
- `websockets` - WebSocket client (standard choice)
- `py-clob-client` - Official Polymarket SDK
- `pyyaml` - Configuration parsing
- `structlog` - Structured logging

All are widely used and actively maintained.

## Network Security

### VPN Usage

If you need to access Polymarket from a geo-restricted region:

1. Use a reputable VPN service
2. Connect to an allowed region
3. Verify connection before starting the bot
4. The bot checks geoblock status on startup

### API Communication

All API communication uses HTTPS/WSS:

- CLOB REST: `https://clob.polymarket.com`
- Gamma: `https://gamma-api.polymarket.com`
- WebSocket: `wss://ws-subscriptions-clob.polymarket.com`

Never modify these URLs to use unencrypted protocols.

## Runtime Security

### File Permissions

Ensure proper file permissions:

```bash
# Secure .env file
chmod 600 .env

# Secure data directory
chmod 700 data/
```

### Process Isolation

Consider running the bot in isolation:

- Use a dedicated VM or container
- Limit network access to required endpoints
- Use a non-root user
- Consider read-only file systems for code

### Monitoring

Monitor for:

- Unexpected API calls
- Unusual trading patterns
- Failed authentication attempts
- High error rates

## Safe Go-Live Checklist

Before enabling LIVE trading:

- [ ] Paper traded for at least 7 days
- [ ] Risk limits are set conservatively
- [ ] Alerts are configured and tested
- [ ] Kill switch has been tested
- [ ] Using a dedicated trading wallet
- [ ] Wallet contains only acceptable loss amount
- [ ] VPN connection is stable (if applicable)
- [ ] Bot has been running stably in PAPER mode
- [ ] All circuit breakers are configured
- [ ] You understand the strategies being used
- [ ] You have reviewed this security document

## Incident Response

If you suspect a security incident:

1. **Trigger the kill switch immediately**
   ```bash
   touch KILL_SWITCH
   # or
   export KILL_SWITCH=1
   ```

2. **Stop the bot**
   ```bash
   # Ctrl+C or
   kill <pid>
   ```

3. **Secure your funds**
   - Transfer remaining funds to a cold wallet
   - Do not reuse the compromised wallet

4. **Investigate**
   - Review logs for unauthorized activity
   - Check transaction history on-chain
   - Identify the attack vector

5. **Remediate**
   - Rotate all credentials
   - Patch any vulnerabilities
   - Consider professional security review

## Contact

If you discover a security vulnerability in this project, please report it responsibly.
