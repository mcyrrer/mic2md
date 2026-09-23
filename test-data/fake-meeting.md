---
llm_model: copilot -p
tags: [hub-and-spoke, incident-triage, kubernetes, mcp, delegated-identity, prompt-logging, data-classification]
---

# AI Platform Hub and Spoke Implementation Kickoff

Okay, I think we're... is everyone here? Priya, you're joining from the car?

No, no, I'm at home, the kids are just being loud. I'll stay on mute when I'm not talking.

Perfect. Ahmed, can you hear us?

Yes, loud and clear. Sorry, I was a minute late, the previous meeting ran over.

No worries. So, um, the purpose of today is basically to agree on how we move forward with AI on the platform side. We've had a lot of teams doing their own thing, everyone's building their own little chatbot with their own API key, and it's getting a bit out of hand. Erik has been looking at the hub and spoke pattern, so I thought we'd start with him walking us through it, and then discuss what a first step could look like. Erik?

## Hub and Spoke Pattern

Yeah, thanks. So, I'll keep it short, I don't have slides, I'll just explain. The idea with hub and spoke is that you have one central component, the hub, which is the only thing that talks to the models and handles all the cross-cutting stuff. Authentication, routing, rate limiting, logging, cost tracking, policy. And then you have the spokes, which are specialised agents. Each spoke does one thing well. So you could have a spoke for incident triage, one for code review, one for answering questions from the internal wiki, and so on.

And the spokes don't talk to each other directly?

Correct. Everything goes through the hub. The hub receives the request, decides which spoke or spokes should handle it, and then combines the result. That's the orchestration part. So the user, or another system, only ever sees the hub.

Which, from a security perspective, I like a lot. One place to enforce things instead of fifteen.

Exactly. That was actually the main reason I started looking at it. Right now we have, I counted, at least seven teams with their own OpenAI or Anthropic keys in their pipelines. Some of them in plain environment variables.

Please don't say that in a recorded meeting.

*laughter*

Well, it's the truth. Anyway. The other benefit is that spokes become quite small and simple. A team that wants to build an agent doesn't need to care about auth or model selection or logging. They just build the spoke, register it with the hub, and they're done.

Can I ask a naive question? How is this different from just having a shared API gateway in front of the model providers?

Good question. A gateway is part of it, the hub would include that. But a plain gateway just forwards requests. The hub also does the orchestration. So if someone asks "why did the checkout service go down last night", the hub can decide to call the incident spoke, which pulls logs, and then maybe the deployment spoke to check what was released, and then put that together into one answer. A gateway wouldn't do that.

Okay, got it. So the hub is kind of the... the brain, and the spokes are the hands.

That's a nice way to put it, yes.

I'd add one thing. The spokes shouldn't necessarily call models directly either. They should call the model through the hub, so we get the cost tracking and logging. Otherwise we're back where we started.

Yes, agreed. Model access is only through the hub. Spokes get a scoped token from the hub.

Okay. I think the concept is clear. Is anyone against going in this direction? Like, does anyone think we should do something completely different?

Not against. I have concerns about the details but the pattern is fine.

I'm for it.

Same.

Good. So let's say that's decided, we go with hub and spoke as the target architecture. Now, how do we actually start? Erik, what's your thinking on where the hub runs?

My suggestion is we run it on the existing Kubernetes cluster, the shared platform one. We already have ingress, we already have the observability stack, we have the identity integration. It doesn't make sense to build something separate.

Separate namespace though.

Separate namespace, network policies, the usual. Yes.

And do we build the hub ourselves or is there something we can use?

That's actually one of the open questions. There are a few open source options now. There's agentgateway, which does a lot of the routing and MCP handling. There's kagent for running agents on Kubernetes. And there are commercial products too. Or we write our own in Python, which honestly isn't that much code for the basic version. I did a small boilerplate over the summer, it's maybe 800 lines.

I would be careful with writing our own. It starts at 800 lines and then two years later it's a product that three people have to maintain.

Fair. I'm not saying we should. I'm saying we should compare.

Okay, so that's not decided today. Erik, can you do a comparison? Build versus the open source options versus buying?

Yes. I'll write it up as an ADR, an architecture decision record, with the options and a recommendation. I can have a draft by next Friday.

Next Friday is the second?

October second, yes.

Good. Include cost estimates if you can, even rough ones.

Will do.

Now, the other thing I want to get out of this meeting is the first spoke. We need a pilot. Something real, something that shows value, but not something where if it goes wrong we end up in the news. Ideas?

## Pilot Use Case

From the product side, the thing I hear most from the support organisation is that they spend a lot of time searching. Searching the knowledge base, searching old tickets. A spoke that can answer "has this happened before and what did we do" would be really valuable.

That involves customer data though. Old tickets have customer names, addresses, sometimes more.

True.

I'm not saying no. I'm saying not as the first one.

What about incident triage? That's internal only. When an alert fires, the spoke pulls the relevant logs, recent deployments, maybe the runbook, and writes a summary in the incident channel. Engineers still decide what to do. It's read-only.

I like that. It's very contained. And the on-call people would love it, honestly. The first ten minutes of every incident is just people pasting Grafana links.

Jonas, how do you feel about that one?

Much better. Logs can contain sensitive stuff too, to be fair, we've had cases where tokens ended up in logs. But it's internal, and if it's read-only, the risk is manageable. I would want to review what data sources it connects to before it goes live.

Okay. Priya, are you okay with the support use case being second?

Yeah, that makes sense. As long as it's on the roadmap. I don't want to go back to the support team and say "maybe next year".

It's on the roadmap. Let's say phase two. So, decision: first spoke is incident triage, read-only.

Can I own that? I've actually already played around with something similar on my own.

Of course. What's a realistic timeline for a proof of concept?

If I can focus on it... two weeks for something that works in a test environment. It won't be pretty.

It doesn't need to be pretty. Two weeks from Monday?

Two weeks from Monday, so around October ninth.

But Ahmed, if the hub isn't decided yet, what will the spoke talk to?

I'll build it against a thin stub. Basically just an interface that looks like the hub. When we decide on the real hub, I swap it out. The spoke itself shouldn't care.

Okay, good. Then we should agree on the interface early. The contract between hub and spoke.

Yes. Let's sit together this week and sketch it.

Thursday?

Thursday afternoon works.

Good. Let's talk about how the spokes access tools and data. Erik, you mentioned MCP?

Right. MCP, Model Context Protocol. It's basically becoming the standard way for agents to talk to tools. So instead of every spoke writing its own integration to Grafana, to GitHub, to Jira, we expose those as MCP servers, and any spoke that's allowed to can use them.

"That's allowed to" is doing a lot of work in that sentence.

[laughs] Yes. That's where the hub comes in again. The hub, or a proxy in front of the MCP servers, checks the identity of the spoke and what it's allowed to call.

And whose identity is it? The spoke's, or the user who triggered it? Because that's the big question for me. If an engineer asks the incident spoke something, and the spoke calls GitHub, is it calling GitHub as the spoke with some service account that can see everything, or as the engineer with the engineer's permissions?

Ideally as the user. On-behalf-of flow with Entra ID. That way the agent can never see more than the person asking.

Ideally. But not all our tools support that.

No. Grafana is fine, GitHub Enterprise is fine, I'm not sure about the older logging platform.

The old one definitely doesn't. It's basic auth or nothing.

So what do we do with that?

For the pilot, I could accept a service account for read-only access to the logging platform, as long as it's scoped to specific indexes and we log every query. But I don't want that to become the pattern. Long term, delegated identity everywhere, or the tool doesn't get connected.

That's reasonable.

Can we write that down as a principle? Delegated user identity by default, service accounts only as a documented exception.

Yes. I'll put it in the security requirements.

Which brings me to that. Jonas, what do you need from a security point of view before this goes into production? Not the pilot, production.

## Security Requirements

A few things. First, a threat model for the hub itself. It's going to be a very attractive target, it has access to everything. Second, a data classification review: which data is allowed to be sent to which model. We probably don't want customer data going to external model providers at all without a proper agreement in place. Third, prompt and response logging, but that's tricky because the logs themselves then contain sensitive data.

Yeah, that's a real problem. If we log every prompt for debugging, we're basically building a new database of everything people asked, including whatever they pasted in.

Exactly. And then someone asks "what's the retention on that" and nobody knows.

What is the retention on that?

Nobody knows. [laughter] No, seriously, I need to check with legal what we're allowed to do and what we have to do. There might be requirements from the EU AI Act as well, depending on how we classify the use cases. I'll check with them.

Okay. And the threat model and classification, when can you have that?

Threat model I can do by end of October. The classification review needs input from the data governance people, so I'd say... end of October is ambitious. Mid-November maybe.

Let's say threat model end of October, and you'll give us a date for the classification once you've talked to data governance.

Fine.

And who owns the logging question? The prompt logging design?

That's kind of a hub design question, so... I mean, it probably ends up with me, but I don't have capacity right now with the ADR.

I can't take it either, I'll be on the triage spoke.

Okay, let's leave that open for now and I'll find someone. But we can't forget it.

## Success Metrics and Cost

Can I bring up something from the product side?

Go ahead.

How do we measure if this is actually working? Because I can already hear the question from management: "we spent X on this, what did we get?" For the incident spoke, what does success look like?

Time to first useful summary in the incident channel, maybe? Right now it's often ten, fifteen minutes before someone has a clear picture.

Mean time to resolution would be the obvious one, but that's affected by so many things.

Yeah, MTTR is too noisy for a pilot. I'd rather have something like: time to first summary, how often engineers found the summary useful, maybe a thumbs up, thumbs down, and how many incidents it was used on.

I like that. Priya, can you define the success metrics for the pilot? Properly, with how we measure them?

Yes. I'll also talk to two or three of the on-call engineers to see what would actually help them. Give me until... mid next week? Wednesday the thirtieth?

Good.

If you define the thumbs up thing, I can build it into the spoke from the start. That's easy.

Great, I'll send it to you.

Now, cost. Erik, you'll do rough estimates in the ADR, but does anyone have a feel for what we're talking about?

For the incident spoke alone, it's small. Maybe a few hundred incidents a month, each one maybe... let's say fifty thousand tokens with the logs. That's nothing. Maybe a few hundred dollars a month, depending on the model.

The bigger cost comes when we roll it out to more teams. If every developer starts using a code review spoke on every PR, that's a very different number.

Which is exactly why I want the hub to track cost per spoke and per team from day one.

Yes, that's in the design. Chargeback per team.

Okay. I'll need to take this to the CFO for budget anyway. Not for the pilot, the pilot is fine within our current budget, but for next year. I'll start that conversation. I'll need the numbers from your ADR, Erik.

You'll have them on the second.

Which model are we going to use? Is that also in the ADR?

Sort of. The point of the hub is that it shouldn't matter too much. The hub can route to different models. Cheap small model for simple things, big model for complex reasoning. But yes, we need to decide which providers we have agreements with.

And that's also a legal and procurement question. We need a data processing agreement with whoever it is. And ideally EU data residency.

Can we also consider running some models ourselves? Open weight models on our own GPUs for the sensitive stuff? Then the customer data never leaves.

We could, but we don't have GPUs in the cluster right now. That's a whole other project.

I know. I'm just saying, for phase two with the support data, it might be the only option Jonas will accept.

[laughs] It might, yes.

Let's park that. It's a real question but it's not for this month. Put it on the list of open questions.

I'm writing it down.

Thanks. One more thing I want to discuss before we wrap up, and that's what the agents are allowed to do. Read-only for the pilot, fine. But at some point someone will want the incident spoke to, I don't know, restart a pod or roll back a deployment.

Oh, they'll ask for that in the first week.

And the answer is no.

For the pilot, yes, no. But long term?

Long term, maybe, with a human approving every action. The agent suggests "I want to roll back to version 4.2", a human clicks approve, then it happens. And it's logged. No autonomous write actions in production. At least not until we have a lot more experience.

I agree with that. The models are good, but they're not "restart production at 3 AM without asking" good.

The hub could handle the approval flow. It's a natural place for it.

Okay, so principle: no autonomous write actions, human in the loop for anything that changes state. Is that agreed?

Agreed.

Agreed.

Yes.

Yes.

Good. Let me just check the time... we have a bit left. Is there anything we haven't covered that someone's worried about?

## Communication and Next Steps

Communication. The teams that already have their own chatbots, how do we tell them? Because if they hear "platform is building a central AI thing" they'll think we're going to shut down their stuff.

Well, in the long run we kind of are. Or at least move it onto the hub.

Right, but we need to say that carefully. Otherwise we'll get pushback before we even start.

I think the message is: we're not taking anything away, we're giving you a better way to do it. You get auth, logging, cost tracking for free. And your existing thing can become a spoke.

That's a good message. But someone needs to actually talk to those teams.

Erik, you have the list of the seven teams?

I have a list. I don't know if it's complete.

Can you share it with Priya? And Priya, could you draft some kind of communication? Doesn't need to go out now, but once the ADR is done.

Sure. I'll draft something, but I'd like Erik to review it for the technical parts.

Yes, of course.

And on that note, those API keys in environment variables. That's not a "once the ADR is done" problem. I want those rotated and moved into the vault now, regardless of the hub.

Fair. I'll send you the list today and you can... I mean, who contacts the teams about the keys?

I'll do it. It's a security issue, it should come from security.

Good. Do we have a deadline for that?

I'd like it done within two weeks. I'll give the teams one week to respond, and then we escalate.

Okay.

One technical thing, sorry, quickly. For the triage spoke, I need access to the test cluster's logs and the Grafana instance. Who do I ask?

Me. I'll set it up. Send me a message after the meeting.

Thanks.

Okay, I think... Jonas, you said something earlier about the EU AI Act. Do we need to worry about that for the pilot?

For an internal incident triage tool, almost certainly not in the high-risk category. But I'd rather have legal confirm that than guess. It's part of what I'll ask them anyway.

Good. Let's not let that slow down the pilot though.

It won't. The pilot's in test, with test data. It's the move to production where we need the answers.

Perfect. Okay, let me try to sum up and then someone correct me if I'm wrong.

We agreed to go with hub and spoke as the target architecture. The hub runs on the shared Kubernetes cluster in its own namespace. All model access goes through the hub. First spoke is incident triage, read-only, Ahmed builds a PoC in about two weeks against a stub. Erik does the ADR on build versus open source versus buy, with cost estimates, by October second. Erik and Ahmed agree the hub-spoke interface on Thursday. Jonas does the threat model by end of October and will come back with a date for the data classification review, and talks to legal about logging retention and the AI Act. Principles: delegated user identity by default, service accounts only as an exception, and no autonomous write actions, human approval for anything that changes state. Priya defines pilot success metrics by the thirtieth and drafts the communication to the teams. Jonas handles getting the API keys moved into the vault within two weeks. I take the budget conversation with the CFO.

That's right. You forgot my list to Priya and Jonas, but that's small.

Right, Erik shares the list of teams with Priya and Jonas today.

And the open questions: build versus buy for the hub, which model providers, self-hosted models for sensitive data, and... the prompt logging design which doesn't have an owner.

Yes, I owe you an owner on that. I'll come back by... let's say end of next week.

One more open question: the old logging platform and delegated identity. We accepted a service account for the pilot, but we need a long-term answer. Either it gets replaced or it doesn't get connected to production agents.

Noted. That's partly a question for whoever owns that platform. Erik, do you know who that is?

It's, um... I think it's the observability team, but they've had a reorg. I'm not sure who the actual owner is anymore. [inaudible] ...Marcus maybe?

Can you find out?

I'll look into it.

Thanks. Okay, I'll book a follow-up in three weeks, when we have the ADR and the PoC. Anything else?

Just, I'm really happy we're doing this properly. The alternative was everyone building their own thing forever.

Agreed.

Me too. Okay, thanks everyone, that was productive. Priya, good luck with the kids.

[laughs] Thanks. Bye.

Bye.

Bye all.

Bye.
