"""Alert engine and email delivery (docs/methods/alerts-v0.1.md).

    scan()        published events, rating facts and model-output changes -> materiality -> one alert per
                  watching user (in-app + live 'alert' stream event); queue 'alerts', every minute
    sweep_instant() red alerts -> instant email rows in email_outbox
    build_digests() daily (07:00 ET) and weekly (Mondays) digest rows
    send_due()    email_outbox -> the configured provider (Resend or Postmark), retries with backoff;
                  without a provider every row becomes 'not_configured' (never a pretend send)

Titles and bodies are code templates over the computed numbers and fact refs (marden.alerts.text for alerts,
marden.alerts.email_templates for emails); no LLM writes any alert or email text.
"""
