Source: https://planetarycomputer.microsoft.com/docs/concepts/sas/ (markdown: https://raw.githubusercontent.com/microsoft/PlanetaryComputerDataCatalog/main/docs/concepts/sas.md), read 2026-10-21. Excerpt:

The data hosted by the Planetary Computer is free for anyone to use. For some data sets, we do require that users utilize a [Shared Access Signature (SAS) Token](https://docs.microsoft.com/en-us/azure/storage/common/storage-sas-overview#how-a-shared-access-signature-works) to authorize requests to [Azure Blob Storage](https://docs.microsoft.com/en-us/azure/storage/blobs/), which stores and serves all of our data assets.

The STAC metatdata API is available to all users and does not require an account or a token to use. All data assets require a token for accessing files on Azure Blob storage.

The token has an expiry time, and when the token has expired a new token must be issued. Rate limiting is put into place in certain cases to limit the amount of egress against our datasets. These limits should be generous - if you find they are getting in the way of your work, please let us know!

Datasets in the Planetary Computer are anonymously accessible: you don't need to supply a subscription key to get a SAS token for downloading the data.
