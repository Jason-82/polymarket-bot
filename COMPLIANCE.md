# Compliance Guidelines

This document outlines compliance considerations for using the Polymarket trading bot.

## Geographic Restrictions

### Polymarket Access

Polymarket is **not available** in certain jurisdictions due to regulatory requirements. This bot:

1. **Checks geoblock status** on startup via the official API
2. **Disables LIVE trading** if you are in a blocked region
3. **Does not circumvent** geographic restrictions

### How Geoblock Works

On startup, the bot calls:
```
GET https://polymarket.com/api/geoblock
```

If the response indicates `blocked: true`:
- LIVE mode is automatically disabled
- READ_ONLY and PAPER modes remain available
- A warning is logged explaining the restriction

### VPN Usage

If you choose to use a VPN:

- The geoblock check will reflect your VPN's exit location
- **You are responsible** for complying with all applicable laws
- This software does not encourage or facilitate circumvention
- Polymarket's Terms of Service apply

## Bot Mode Defaults

The bot is designed with safety-first defaults:

| Setting | Default | Reason |
|---------|---------|--------|
| Mode | `READ_ONLY` | Prevents accidental trading |
| Strategies | Disabled | Must be explicitly enabled |
| Risk limits | Conservative | Protects capital |
| Kill switch | Enabled | Automatic safety stop |

### Enabling Live Trading

To trade with real money, you must:

1. Explicitly set `--mode LIVE`
2. Pass the geoblock check
3. Configure a private key
4. Enable at least one strategy
5. Add tokens to trade

This multi-step process prevents accidental live trading.

## Terms of Service

By using this bot, you agree to:

1. **Polymarket's Terms of Service**: https://polymarket.com/terms
2. **Polygon Network Terms**: Applicable blockchain terms
3. **Your local regulations**: You are responsible for compliance

## Know Your Customer (KYC)

Polymarket may require KYC verification for certain activities. This bot:

- Does not handle KYC processes
- Does not store or transmit identity documents
- Requires you to complete KYC directly with Polymarket

## Market Manipulation

This bot is designed for **legitimate trading purposes**. It must not be used for:

- Market manipulation
- Wash trading
- Price fixing
- Any form of fraud

The audit logging feature exists to demonstrate trading intent, not to enable manipulation.

## Tax Considerations

Trading on Polymarket may have tax implications:

- You are responsible for tracking gains/losses
- The bot stores trade history that may assist with record-keeping
- Consult a tax professional for guidance

## Data Privacy

The bot collects and stores:

| Data | Purpose | Storage |
|------|---------|---------|
| Market data | Analysis, backtesting | Local SQLite |
| Trade history | Record-keeping | Local SQLite |
| Strategy decisions | Audit trail | Local SQLite |

This data is stored locally and is not transmitted to third parties (except Polymarket APIs for trading).

## Responsible Trading

### Risk Awareness

Prediction markets involve significant risk:

- You can lose your entire investment
- Prices can move quickly and unexpectedly
- Past performance does not predict future results
- The strategies in this bot are for educational purposes

### Position Limits

The bot enforces position limits to prevent:

- Over-concentration in single markets
- Excessive total exposure
- Runaway losses

### Circuit Breakers

Automatic stops protect you from:

- Flash crashes
- Strategy malfunctions
- API failures
- Network issues

## Disclaimers

### No Financial Advice

This software does not provide:

- Investment advice
- Trading recommendations
- Guarantees of profit

The baseline strategies are educational examples only.

### No Warranty

This software is provided "as is" without warranty of any kind.

### Limitation of Liability

The authors are not responsible for:

- Financial losses
- Technical failures
- Regulatory issues
- Any other damages

## Compliance Checklist

Before using this bot for live trading:

- [ ] I have verified I am not in a blocked jurisdiction
- [ ] I have read Polymarket's Terms of Service
- [ ] I understand the risks involved in trading
- [ ] I have consulted applicable laws in my jurisdiction
- [ ] I am prepared to lose any funds I deposit
- [ ] I have set appropriate risk limits
- [ ] I understand this is not financial advice

## Questions

For compliance questions about Polymarket:
- Contact Polymarket directly via their official channels

For legal questions:
- Consult a qualified attorney in your jurisdiction
