# Contributing to sardinetracker

First, thank you for being here. Whether you found this project because you're sick and frustrated and recognized yourself in the README, or because you're a developer who wants to build something useful, or both: welcome.

## Who Can Contribute

Everyone. Lived experience of diagnostic complexity is a qualification, not a footnote. If you've spent years trying to figure out what is wrong, or trying to organize your data so that a doctor can see the patterns you do, you understand the problem this tool is trying to solve better than most.

Specific areas where help is genuinely needed:

- Additional data import formats (Fitbit, Garmin, Google Fit, Samsung Health)
- Period/cycle/pregnancy tracking integration
- Perimenopause and menopausal symptom tracking integration
- Accessibility improvements
- Windows testing and compatibility fixes
- More correlation analysis methods
- PDF export improvements
- Translations
- Documentation and plain-language tutorials
- Adaptations for other hard-to-diagnose conditions beyond lupus

## Before You Start

Open an issue before beginning work on a major feature. Not to gatekeep — just to avoid two people building the same thing simultaneously, and to make sure the direction fits the project.

For bugs, open an issue with:

- What you expected to happen
- What actually happened
- Your OS, Python version, and browser
- Any error messages from the terminal

## How to Submit Changes

1. Fork the repository
2. Create a branch with a descriptive name (`fix-uv-backfill-longitude`, `add-garmin-import`)
3. Make your changes
4. Test them — actually run the app, add some data, make sure nothing broke
5. Submit a pull request with a clear description of what you changed and why
6. **Note:** Don't commit `config/custom_weights.json` - it's in `.gitignore` for privacy

## Code Style

Nothing formal. Readable over clever. Comments where the logic isn't obvious. If you're using an LLM to help write code, that's fine, this whole project was built that way, just make sure you understand what you're submitting. Nobody knows everything, and we all make mistakes. Even AI.

## A Note on Scope

This tool was built for one specific use case and expanded from there. It is designed with my use case in mind primarily. Not every feature request will be the right fit, and that's okay. If your needs diverge significantly from the core use case, the AGPL-3.0 license means you can fork it and build your own thing. That's what it's there for.

## Contact

For questions that don't fit an issue: <alaric.moore@pm.me>

Response times may vary. This is a one-person project maintained between doctor appointments and fixing machines and building terrariums.

Take care of yourself out there.
