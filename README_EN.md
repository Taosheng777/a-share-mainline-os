# A-Share Mainline Research and Decision Support

`a-share-mainline-os` is a Skill suite for researching the lifecycle of China A-share market themes and supporting investment decisions. The repository is currently being assembled as `v0.1-beta`. This commit establishes the licensing, third-party dependency, and financial-tool boundaries; installation and clean-start packaging will follow in the next phase.

## License boundary

- Original code and documentation in this repository are licensed under the [MIT License](LICENSE).
- Data capabilities interoperate with the external project [simonlin1212/a-stock-data](https://github.com/simonlin1212/a-stock-data), authored by Simon Lin and licensed under the [Apache License 2.0](https://github.com/simonlin1212/a-stock-data/blob/main/LICENSE).
- This repository does not copy, modify, redistribute, or relicense `a-stock-data` source code.
- See the [dependency license audit](docs/依赖许可证清单.md) and [NOTICE](NOTICE) for the recorded boundary.

## Third-party data and services

This project provides local tools and adapters only. It does not include market data, filings, research reports, news content, account credentials, API keys, or private datasets. Adapters target public third-party webpages or services and do not imply an official API or a data-redistribution license; interfaces may change, become rate-limited, or stop working at any time. Users are responsible for complying with each provider's terms, permissions, rate limits, and applicable requirements. Third-party names and trademarks belong to their respective owners; no affiliation or endorsement is implied.

External components with unverified licenses, including some iWenCai SkillHub tools and a private iFinD helper, are excluded from the distribution. If configured separately by a user, they remain optional runtime adapters and must fail with an explicit degradation state when unavailable.

## Disclaimer

This project is for research and decision support only. The provider is not a licensed securities investment advisory institution. Neither the project nor its output constitutes investment advice, and it does not place orders. Users assume all risk. AI-generated content and third-party data may contain errors, omissions, or delays and must be independently verified. Data sources and as-of dates are those reported by each runtime output.
