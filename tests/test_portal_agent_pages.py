"""The agent pages (spec 2026-11-17 "What the person sees", Errors; decided while planning: item 32)."""

from portal.store.base import AgentObserved

AGENT = {"schema": "agent/v1", "display_name": "Calendar helper", "model": "chat-default", "instructions": "Find free time.", "starters": ["Thursday?"]}


def observe(portal, agent=AGENT, problem=None, repo="acme/notes"):
    portal.store.replace_agent_observed([AgentObserved(repo, "a" * 40, None if problem else agent, problem, portal.clock())],
                                        observed_at=portal.clock())


def form(portal, who="owner", **changes):
    data = {"csrf_token": portal.csrfs[who], "display_name": "Calendar helper", "model": "chat-default", "instructions": "Find free time.",
            "starter": ["When is everyone free?", "", "", ""], **changes}
    return portal.client(who).post("/apps/acme/notes/agent", data=data)


def agent_requests(portal):
    return [r for r in portal.store.requests_for_repo("acme/notes") if r.kind == "agent_definition"]


def test_build_an_agent_links_the_template_and_lists_my_agent_repos(portal):
    observe(portal)
    page = portal.client("owner").get("/agents/new")
    assert page.status_code == 200
    assert 'href="https://github.com/acme/golden-road-agent-template/generate"' in page.text
    assert "acme/notes" in page.text and 'href="/apps/acme/notes/agent"' in page.text
    assert "acme/tools" not in page.text  # an admin repo of mine, but not an agent repo
    assert 'aria-current="page"><span>Build an agent</span>' in page.text


def test_build_an_agent_lists_nobody_else_s_agent_repos(portal):
    observe(portal)
    page = portal.client("other").get("/agents/new").text
    assert "acme/notes" not in page and "No agent repo of yours yet" in page


def test_the_template_link_comes_from_the_settings_never_the_request(portal):
    page = portal.client("owner").get("/agents/new?org=evil&host=evil.example.test").text
    assert "evil" not in page and "https://github.com/acme/golden-road-agent-template/generate" in page


def test_the_agent_page_says_when_a_repo_isn_t_an_agent_repo(portal):
    page = portal.client("owner").get("/apps/acme/notes/agent")
    assert page.status_code == 200 and "isn&#39;t an agent repo" in page.text and "golden-road-agent-template/generate" in page.text
    answer = form(portal)
    assert answer.status_code == 409 and "isn&#39;t an agent repo" in answer.text
    assert agent_requests(portal) == []


def test_the_form_starts_from_the_observed_agent(portal):
    observe(portal)
    page = portal.client("owner").get("/apps/acme/notes/agent").text
    assert 'value="Calendar helper"' in page and "Find free time." in page and 'value="Thursday?"' in page
    assert '<option value="chat-default" selected>Default chat model</option>' in page


def test_a_save_records_an_approved_agent_definition_and_says_so(portal):
    observe(portal)
    answer = form(portal)
    assert answer.status_code == 303 and answer.headers["location"] == "/apps/acme/notes/agent"
    [req] = agent_requests(portal)
    assert req.state == "approved" and req.draft["starters"] == ["When is everyone free?"] and req.requested_by == portal.people["owner"].id
    page = portal.client("owner").get("/apps/acme/notes/agent").text
    assert "the writer opens or updates the pull request on its next pass" in page


def test_an_open_pr_is_shown_and_saving_again_is_another_request(portal):
    observe(portal)
    form(portal)
    [req] = agent_requests(portal)
    portal.store.claim_approved(now=portal.clock())
    portal.store.mark_pr_open(request_id=req.id, branch="portal/agent-definition", pr_number=4, pr_url="https://github.com/acme/notes/pull/4",
                              head_sha="a" * 40, base_sha="b" * 40, now=portal.clock())
    page = portal.client("owner").get("/apps/acme/notes/agent").text
    assert 'href="https://github.com/acme/notes/pull/4"' in page and "saving again updates it" in page
    assert form(portal, display_name="Second").status_code == 303
    assert len(agent_requests(portal)) == 2


def test_the_writer_s_retried_error_is_shown_escaped(portal):
    observe(portal)
    form(portal)
    req = portal.store.claim_approved(now=portal.clock())
    portal.store.record_request_error(request_id=req.id, error="no_agent_app: PORTAL_AGENT_APP_ID is not set <b>", now=portal.clock())
    page = portal.client("owner").get("/apps/acme/notes/agent").text
    assert "The writer is retrying: no_agent_app: PORTAL_AGENT_APP_ID is not set &lt;b&gt;." in page and "<b>" not in page


def test_a_failed_definition_is_shown(portal):
    observe(portal)
    form(portal)
    req = portal.store.claim_approved(now=portal.clock())
    portal.store.mark_failed(request_id=req.id, reason="pr_closed", now=portal.clock())
    assert "The last save could not be recorded: pr_closed." in portal.client("owner").get("/apps/acme/notes/agent").text


def test_the_instructions_are_escaped(portal):
    observe(portal, agent={**AGENT, "instructions": "</textarea><script>alert(1)</script>"})
    page = portal.client("owner").get("/apps/acme/notes/agent").text
    assert "<script>alert(1)" not in page and "&lt;/textarea&gt;&lt;script&gt;alert(1)&lt;/script&gt;" in page


def test_a_bad_form_is_a_400_naming_the_field(portal):
    observe(portal)
    answer = form(portal, model="other", starter=["a", "b", "c", "d", "e"])
    assert answer.status_code == 400
    assert "Model: pick one of the platform&#39;s models." in answer.text and "Conversation starters: at most 4" in answer.text
    assert agent_requests(portal) == []


def test_a_bad_csrf_token_is_a_403(portal):
    observe(portal)
    assert form(portal, csrf_token="not-the-token").status_code == 403
    assert agent_requests(portal) == []


def test_a_portal_admin_who_isn_t_the_repo_s_admin_sees_the_page_but_can_t_save(portal):
    observe(portal)
    page = portal.client("admin").get("/apps/acme/notes/agent")
    assert page.status_code == 200 and "Only an admin of the repository" in page.text
    assert form(portal, "admin").status_code == 403
    assert agent_requests(portal) == []


def test_someone_else_gets_a_404(portal):
    observe(portal)
    assert portal.client("other").get("/apps/acme/notes/agent").status_code == 404
    assert form(portal, "other").status_code == 404


# ---- the app page's Agent card, the requests list and the request page (decided while planning: item 33) ----

def test_the_app_page_has_an_agent_card_for_an_agent_app(portal):
    observe(portal)
    page = portal.client("owner").get("/apps/acme/notes").text
    assert "<h2>Agent</h2>" in page and "Calendar helper" in page and "chat-default" in page and "Find free time." in page
    assert 'href="/apps/acme/notes/agent"' in page and "No tools: the model alone, through the platform gateway." in page


def test_the_card_lists_what_the_environment_s_binding_gives_the_runtime(portal):
    from portal_fakes import binding_request
    observe(portal)
    binding_request(portal.store, portal.people, portal.clock(), environment="prod", mcp=("notes",), skills=(("incident-triage", "c" * 40),))
    prod = portal.client("owner").get("/apps/acme/notes?environment=prod").text
    card = prod[prod.index("<h2>Agent</h2>"):]
    card = card[:card.index("</section>")]
    assert "What the runtime gets in prod" in card
    assert "graph_read" in card and "databricks_query" in card and "MCP server Notes" in card and "incident-triage" in card
    test = portal.client("owner").get("/apps/acme/notes?environment=test").text
    assert "graph_read" not in test and "No tools: the model alone, through the platform gateway." in test


def test_the_card_s_instructions_are_escaped(portal):
    observe(portal, agent={**AGENT, "instructions": "<script>alert(1)</script>"})
    page = portal.client("owner").get("/apps/acme/notes").text
    assert "<script>alert(1)" not in page and "&lt;script&gt;alert(1)&lt;/script&gt;" in page


def test_the_card_shows_only_the_first_lines(portal):
    observe(portal, agent={**AGENT, "instructions": "one\ntwo\nthree\nfour is hidden"})
    page = portal.client("owner").get("/apps/acme/notes").text
    assert "one\ntwo\nthree…" in page and "four is hidden" not in page


def test_no_card_for_an_app_that_isn_t_an_agent(portal):
    assert "<h2>Agent</h2>" not in portal.client("owner").get("/apps/acme/notes").text


def test_a_portal_admin_sees_the_card_without_the_link(portal):
    observe(portal)
    page = portal.client("admin").get("/apps/acme/notes").text
    assert "<h2>Agent</h2>" in page and 'href="/apps/acme/notes/agent"' not in page


def test_a_card_says_when_agent_yaml_is_broken(portal):
    observe(portal, problem="agent_yaml_invalid")
    assert "it isn&#39;t a valid agent/v1 with one of the platform&#39;s models" in portal.client("owner").get("/apps/acme/notes").text


def test_my_requests_name_an_agent_definition_and_its_page_leads_to_the_agent_page(portal):
    observe(portal)
    form(portal)
    [req] = agent_requests(portal)
    assert "Agent (Calendar helper)" in portal.client("owner").get("/requests").text
    answer = portal.client("owner").get(f"/requests/{req.id}")
    assert answer.status_code == 303 and answer.headers["location"] == "/apps/acme/notes/agent"


# ---- save()'s other refusals and the stored name (Round 7 review) ----

def test_a_decommissioned_app_s_agent_can_t_be_saved(portal):
    from test_portal_lifecycle_pages import DECOMMISSIONED, decommissioned, with_lifecycle
    with_lifecycle(portal)
    observe(portal)
    decommissioned(portal)
    page = portal.client("owner").get("/apps/acme/notes/agent").text
    assert "The app is decommissioned" in page and "disabled>Save</button>" in page
    answer = form(portal)
    assert answer.status_code == 409 and DECOMMISSIONED in answer.text
    assert agent_requests(portal) == []


def test_a_decommission_that_lands_during_the_save_is_a_409(portal, monkeypatch):
    from test_portal_lifecycle_pages import DECOMMISSIONED, decommissioned, with_lifecycle
    with_lifecycle(portal)
    observe(portal)
    create = portal.store.create_agent_definition

    def racing(**kw):  # the decommission lands after the page's check and before the insert: the store refuses it
        decommissioned(portal)
        return create(**kw)

    monkeypatch.setattr(portal.store, "create_agent_definition", racing)
    answer = form(portal)
    assert answer.status_code == 409 and DECOMMISSIONED in answer.text
    assert agent_requests(portal) == []


def test_a_save_stores_the_repo_as_github_spells_it(portal):
    from portal.store.base import RepoAdmin
    portal.store.replace_repo_admins([RepoAdmin("Acme/Notes", "owner-gh", 1001, portal.clock())], observed_at=portal.clock())
    observe(portal, repo="Acme/Notes")
    answer = form(portal)  # posted to /apps/acme/notes/agent
    assert answer.status_code == 303 and answer.headers["location"] == "/apps/Acme/Notes/agent"
    [req] = agent_requests(portal)
    assert req.repo == "Acme/Notes"


def test_a_stale_admin_gets_a_404_and_nothing_is_saved(portal):
    observe(portal)
    portal.clock.advance(minutes=11)  # past repo_admins_stale_after (10 minutes); the session stays valid
    assert portal.client("owner").get("/apps/acme/notes/agent").status_code == 404
    assert form(portal).status_code == 404
    assert agent_requests(portal) == []


def test_build_an_agent_says_when_my_repositories_are_stale(portal):
    observe(portal)
    portal.clock.advance(minutes=11)
    page = portal.client("owner").get("/agents/new").text
    assert "too old to rely on" in page and 'href="/apps/acme/notes/agent"' not in page


def test_an_older_open_pr_is_shown_while_the_newest_save_waits(portal):
    observe(portal)
    form(portal)
    [first] = agent_requests(portal)
    portal.store.claim_approved(now=portal.clock())
    portal.store.mark_pr_open(request_id=first.id, branch="portal/agent-definition", pr_number=4, pr_url="https://github.com/acme/notes/pull/4",
                              head_sha="a" * 40, base_sha="b" * 40, now=portal.clock())
    portal.clock.advance(seconds=1)
    form(portal, display_name="Second")
    page = portal.client("owner").get("/apps/acme/notes/agent").text
    assert 'value="Second"' in page  # the form starts from the newest save
    assert 'href="https://github.com/acme/notes/pull/4"' in page
    assert "the writer opens or updates the pull request on its next pass" in page


def test_the_card_names_what_the_runtime_gets(portal):
    observe(portal)
    assert "What the runtime gets in test" in portal.client("owner").get("/apps/acme/notes").text


def test_an_agent_with_an_identity_shows_its_graph_egress(portal):
    """The final review's I1: compose gives an agent with an Azure identity graph.microsoft.com on 443 (graph_read), and the page
    says what compose writes: on the card's graph_read and in the Connections egress line. Any other app's line is as before."""
    from portal_fakes import binding_request
    binding_request(portal.store, portal.people, portal.clock(), environment="prod")
    before = portal.client("owner").get("/apps/acme/notes?environment=prod").text
    assert "graph.microsoft.com" not in before
    observe(portal)
    prod = portal.client("owner").get("/apps/acme/notes?environment=prod").text
    card = prod[prod.index("<h2>Agent</h2>"):]
    card = card[:card.index("</section>")]
    assert "egress to graph.microsoft.com on 443" in card
    line = prod[prod.index("Egress on 443 to:"):]
    assert '<span class="mono">graph.microsoft.com</span>' in line[:line.index("</p>")]
