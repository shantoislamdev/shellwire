# Contributing to Shellwire

First off, thank you for considering contributing to Shellwire! It's people like you that make open-source software such a great community to learn, inspire, and create.

By participating in this project, you agree to abide by our [Code of Conduct](CODE_OF_CONDUCT.md).

## How Can I Contribute?

### Reporting Bugs

Before creating bug reports, please check the existing issues to see if the problem has already been reported. When you are creating a bug report, please include as many details as possible:
* Use the provided Bug Report template.
* Provide specific steps to reproduce the issue.
* Mention your OS, Python version, and Shellwire version.

### Suggesting Enhancements

If you have an idea for a new feature or an improvement:
* Use the provided Feature Request template.
* Describe the current behavior and the behavior you expect.
* Explain why this enhancement would be useful to most users.

### Pull Requests

1. Fork the repo and create your branch from `main`.
2. If you've added code that should be tested, add tests.
3. If you've changed APIs or features, update the documentation in the `docs/` folder.
4. Ensure the test suite passes.
5. Make sure your code lints and is formatted properly (follow existing code style).
6. Issue that pull request!

## Local Development Setup

To set up your local environment, you can use our built-in runner scripts which automatically create a virtual environment (`venv`) and install dependencies for you.

### Unix / Linux / macOS
```bash
# Bootstraps the daemon inside the venv
./run.sh

# Runs the test suite inside the venv
./test.sh
```

### Windows
```cmd
# Bootstraps the daemon inside the venv
run.bat

# Runs the test suite inside the venv
test.bat
```

### Manual Setup
If you prefer not to use the runner scripts, you can set it up manually:
```bash
git clone https://github.com/shantoislamdev/shellwire.git
cd shellwire
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
pip install -e ".[dev]"
```

### Running Tests

We use `pytest` for running our test suite. Make sure all tests pass before submitting your PR.
```bash
pytest
```
To run tests with coverage:
```bash
pytest --cov=shellwire
```

Thank you for contributing!
