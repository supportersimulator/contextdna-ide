/**
 * Consult Brain Command for Raycast
 */

import {
  Action,
  ActionPanel,
  Detail,
  Form,
  showToast,
  Toast,
  useNavigation,
} from "@raycast/api";
import { useState } from "react";
import { consult } from "./api";

export default function Command() {
  const [isLoading, setIsLoading] = useState(false);
  const { push } = useNavigation();

  async function handleSubmit(values: { task: string }) {
    if (!values.task.trim()) {
      showToast({ style: Toast.Style.Failure, title: "Task description required" });
      return;
    }

    setIsLoading(true);

    try {
      const response = await consult(values.task.trim());

      push(<ContextView task={values.task} context={response.context} />);
    } catch (error) {
      showToast({
        style: Toast.Style.Failure,
        title: "Failed to consult brain",
        message: error instanceof Error ? error.message : "Unknown error",
      });
    } finally {
      setIsLoading(false);
    }
  }

  return (
    <Form
      isLoading={isLoading}
      actions={
        <ActionPanel>
          <Action.SubmitForm title="Consult Brain" onSubmit={handleSubmit} />
        </ActionPanel>
      }
    >
      <Form.TextField
        id="task"
        title="Task"
        placeholder="What are you about to work on?"
        autoFocus
      />
      <Form.Description
        title="About"
        text="Get relevant context from your learning history before starting a task. The brain will search for related wins, fixes, and patterns."
      />
    </Form>
  );
}

function ContextView({ task, context }: { task: string; context: string }) {
  const markdown = `# 🧠 Context for: ${task}

---

${context || "*No relevant context found. This might be a new area for you!*"}

---

*Generated from your Context DNA learning history*
`;

  return (
    <Detail
      markdown={markdown}
      actions={
        <ActionPanel>
          <Action.CopyToClipboard title="Copy Context" content={context} />
          <Action.OpenInBrowser
            title="Open in Dashboard"
            url={`http://localhost:3457/consult?task=${encodeURIComponent(task)}`}
          />
        </ActionPanel>
      }
    />
  );
}
