# Contributing

Thanks for your interest in contributing to CISO Approval Bot!

## Getting Started

1. Fork this repository
2. Create a feature branch: `git checkout -b feature/your-feature`
3. Make your changes
4. Test locally with your own Slack workspace
5. Commit with clear messages: `git commit -m "Add: brief description"`
6. Push and open a pull request

## Development Setup

```bash
git clone https://github.com/YOUR_USERNAME/ciso-approval-bot.git
cd ciso-approval-bot
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Fill in .env with your test credentials
```

## Guidelines

- Keep changes focused — one feature or fix per PR
- Don't commit `.env` files or any credentials
- Update the README if you change configuration or behavior
- Follow existing code style (PEP 8)
- Test with a non-production Slack workspace before submitting

## Reporting Issues

Open an issue with:
- What you expected to happen
- What actually happened
- Steps to reproduce
- Relevant log output (redact any tokens/IDs)

## License

By contributing, you agree that your contributions will be licensed under the Apache 2.0 License.
