"""ONBOARDER role: publishes the tenant SotaWiki and opens the client-workflow onboarding PR."""

from flow_agent.roles.onboarder.runner import OnboardingError, run_onboarder

__all__ = ["OnboardingError", "run_onboarder"]
