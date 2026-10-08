# Set up Python

Pear Review's reviewer runs in a small Python service on your machine. It needs
**Python 3.10 or later**.

**Set Up Python Environment** finds a Python (the one the Python extension has
selected, if you use it), makes a private environment for Pear Review, and
installs what the service needs. It takes a few minutes the first time, and
nothing outside that environment changes.

Already have an environment with the packages? Set `pearReview.pythonPath` to
its interpreter instead.

The reviewer itself is a model: Ollama on your machine by default, Claude
through the Anthropic API, or any OpenAI-compatible endpoint (set the
provider, and its key, in the chat's ⚙).
