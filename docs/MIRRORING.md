# UHRET repository synchronization

UHRET uses GitHub-native public forks. Active development stays in the original repositories. One Actions workflow in this repository checks upstream Git references every hour at minute 23 UTC. The same minute applies in Türkiye. GitHub can delay or drop scheduled jobs during load. Public scheduled workflows can be disabled after 60 days without repository activity. Review the Actions page after an extended pause and re-enable the workflow there if GitHub deactivates it. [GitHub schedule documentation](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)

| Destination | Upstream | Rollout |
| --- | --- | --- |
| EarthSona | grassified/EarthSona | Automatic |
| ARES-Mission-Control | baban9999ytr/ARES-Mission-Control | Automatic |
| QK-norm-UHRET | eeea2222/QK-norm-UHRET | Automatic |
| solar-pulse | ilyascinar593-ops/solar-pulse | Automatic. Upstream currently publishes documentation only. |
| caly-full | Hakanefe25/caly-full | Automatic |
| caly-demo | Hakanefe25/caly-demo | Automatic |

One matrix job handles each enabled fork. GitHub-native fork relationships keep provenance. Ref updates use ordinary Git fast-forwards instead of the REST fork sync endpoint, which accepts one branch and does not synchronize tags. The workflow can create upstream branches and tags, retains destination-only refs and halts a repository when an existing branch diverges or a tag is replaced. It never forces a push or checks out or runs upstream code. Each Git ref update uses the server's normal non-forcing safety check. Updates to several refs can partially succeed if GitHub rejects individual refs, so the workflow verifies and reports the resulting refs and retries only normal outstanding updates on later runs.

The workflow covers Git branches and tags. Issues, pull requests, release attachments, Packages, secrets, GitHub metadata and production integrations are outside its scope. New LFS pointers and submodule entries stop the affected job. A GitHub fork keeps existing Git objects and notices, but a matching ref alone doesn't establish that the fork can retrieve every LFS object. Verify LFS availability and quota separately before enabling LFS replication. The script checks common credential patterns in incoming objects and changes to existing license and source notices. These checks cannot determine an asset's rights or detect every secret. Review any known rights restriction before enabling a destination.

## CALY asset-rights confirmation

The UHRET requester confirmed that the relevant rights had been verified for public GitHub forks of both CALY repositories. This covers the narrator portrait, derivative scenes and relevant assets in historical demo packages. The original notices remain as published. See the [full asset register](https://github.com/Hakanefe25/caly-full/blob/main/SOURCES.md) and [demo asset register](https://github.com/Hakanefe25/caly-demo/blob/main/SOURCES.md).

## Installation and onboarding

1. The private [UHRET Repository Sync installation](https://github.com/organizations/oztimurlarmtaluhret/settings/installations/186111855) uses selected repository access for EarthSona, ARES-Mission-Control, QK-norm-UHRET, solar-pulse, caly-full and caly-demo. An organization Owner manages the installation. Keep write access scoped to verified organization forks. The registered permissions are Administration read for the Actions setting check, Contents read/write for refs, and Workflows read/write for upstream workflow files.
2. The central repository stores the App ID in the Actions variable UHRET_MIRROR_APP_ID and the private key in the [Actions secret](https://github.com/oztimurlarmtaluhret/.github/settings/secrets/actions) UHRET_MIRROR_APP_KEY. Each sync job mints a short-lived token for its own target. Use the existing credentials when adding destinations. Revoke and replace the key if exposed.
3. Confirm the target has disabled Actions and Pages, no hosting integration or deploy record, and the same visibility and parent recorded in config/mirrors.json. Review [Synchronize project forks](https://github.com/oztimurlarmtaluhret/.github/actions/workflows/sync-mirrors.yml). For initial real validation run the workflow on main with dry_run disabled and inspect the actual sync job. A passing plan alone does not verify synchronization.
4. For any future addition, verify the publishing rights, create a native fork, confirm its upstream and repository IDs, and extend the selected installation to that fork. Enable the mapping through a reviewed PR. Once the hosted workflow has verified its refs, add it to the public portfolio.

The automatic mode uses observed upstream heads and never asks for per-commit approval. Config mode snapshot retains optional exact approved_refs comparison for an exceptional repository. Set mode to disabled to pause an individual destination. Diagnostic runs may use dry_run to verify proposed Git updates without writing them.

## Monitoring and recovery

A missing App ID or private-key secret fails each enabled target before token generation, for scheduled and manual runs. The per-repository job logs and Actions step summary report source, destination, before and after ref states, updates, unchanged refs, skipped items and reasons when checks stop an update. Watch the infrastructure repository and select failed Actions workflow notifications under [GitHub notification settings](https://github.com/settings/notifications). If the scheduler is disabled after inactivity, re-enable it from the Actions workflow page. A scheduled run is only verified when the Actions history shows the scheduled event and successful target jobs.

For an unexpected deployment, privacy change, rights question, divergence or suspected credential, disable the affected mapping and inspect the report without disclosing the triggering content. Handle branch rewrites and tag replacement as explicit maintainer decisions. Do not recover by forcing changes from the upstream. In an emergency, disable the workflow at its Actions page, cancel active runs and suspend or remove the App installation from the affected destinations. To roll back the architecture, revert the infrastructure PR. Existing fork versions and original deployments are independent of that revert.
