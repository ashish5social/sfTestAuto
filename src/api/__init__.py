"""
API test handler (clean separation from UI).

This package is the home for everything used by API-based tests (like TC3):
    - SFApiClient:   OAuth auth, SObject CRUD, Integration Procedure calls
    - APITracker:    step/call tracking parallel to UI's StepTracker
    - api_reporter:  HTML report generator showing request/response cards
                     instead of screenshots/videos

API tests MUST NOT import from src.core.playwright_helpers or
src.core.html_reporter — those are UI-only. Shared infrastructure
(config, sf_connector for SOQL queries) is in src.core.
"""
