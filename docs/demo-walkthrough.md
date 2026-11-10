# BRK301 demo walkthrough

## Story

Begin with one customer-service agent that understands a hotel guest's request.
Introduce each source of context separately, then show how they work together.
End with two agents with accounts, each with its own Agent 365 identity,
collaborating in one Microsoft Teams chat. A mailbox is one communication
surface of an identity, not the definition of the agent.

## Act 1: Customer service, one IQ at a time

Use the [customer-service project](../demos/customer-service/README.md).
The implemented display name is **Hotel Guest Services** and the stable technical
name is `hotel-servicing-agent`.

| Beat | Example request | Evidence to show |
| --- | --- | --- |
| Foundry IQ | "What are this property's cancellation rules and accessibility amenities?" | Property- and date-scoped policy or property documents with citations. |
| Fabric IQ | "Find this guest's reservation and show its current dates, status, and property." | Actual structured reservation rows and the resolved property, not a property guessed from the prompt. |
| Work IQ | "What relevant context is available in the guest's earlier correspondence?" | Authorized workplace context, with gaps stated explicitly. Seed fictional correspondence in your own tenant first. |
| Combined | "Using the reservation and the property's policy, explain the available options." | A grounded answer combining the appropriate sources without inventing missing facts. |

Replace generic guest/property wording with identifiers from the generated
synthetic dataset. Do not promise that arbitrary wording will trigger a
particular tool; rehearse and inspect actual tool evidence. The optional voice
frontend is another entry point to the same customer-service orchestration.
Keep the opening read-only; any optional reservation action needs its own
explicit confirmation and configured mutation backend.

Transition: **"The same context can support agents that coordinate and act,
not just answer a question."**

## Act 2: Two agents in the same chat

Use the [payroll project](../demos/payroll-autopilots/README.md). Add the two
independently hired agents and the configured human approvers to a dedicated
**Payroll Funding** Microsoft Teams group chat. Bind its actual native chat ID
in private environment configuration; a chat title alone is not a binding.

The synthetic starting state is:

| Account or obligation | Amount |
| --- | --- |
| Payroll account | USD 10,000 |
| Reserve account | USD 50,000 |
| Recorded payroll obligation | USD 25,000 |
| Funding shortfall | USD 15,000 |

1. Start a deliberate, isolated rehearsal request such as: "Check whether the
   recorded payroll is funded. If funding is needed, coordinate with Hotel
   Treasurer and ask the configured approvers before transferring funds."
2. Show **Hotel Payroll** recognizing the USD 15,000 shortfall and contacting
   **Hotel Treasurer** in the shared conversation.
3. Show **Hotel Treasurer** asking the configured human approvers for approval
   to move USD 15,000 from reserve to payroll. The names come from your tenant;
   the source's example personas are not authorization identities.
4. An authorized human replies naturally in the bound approval conversation.
   The runtime must authenticate the decision and correlate it to the pending
   request. A quoted "Approved" or an agent's own message is not human approval.
5. Show the authorized result: payroll USD 25,000; reserve USD 35,000; exactly
   one USD 15,000 ledger transaction. The agents report the outcome in chat.

The payoff is proactive agent-to-agent coordination after the initiating
request, with a human approval boundary before the financial effect. Do not
describe this as an unattended scheduler or a real bank transfer.

## Rehearsal and reset

Read current balances and the pending request before every run. If payroll is
already funded, the correct result is "no transfer needed", not a repeat
transfer. Use an explicitly configured isolated demo run for repeatable
rehearsals; never reset or replay the shared live ledger.

The collaboration runtime has explicit `three_iq` and `collaboration` modes.
The default three-IQ path requires successful IQ evidence and physical
projection checks before governed model-driven actions. The final chat demo
uses the explicitly selected collaboration mode; do not claim every IQ was
consulted there without showing the actual calls.

Check for duplicate replies or repetitive agent handoffs during rehearsal.
Do not equate messages claiming success with ledger verification. Confirm the
ledger and, when demonstrating Fabric analytics, confirm projection freshness
separately. A successful provider send receipt is not proof of recipient delivery.

## Presentation assets

This revision deliberately excludes the live tenant screenshot and chat
transcripts. Use a sanitized recording or recreated fictional conversation for
public material. Session slide visuals can be added after the deck is readable
and their customer-facing content has been reviewed.
