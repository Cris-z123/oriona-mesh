"use client";

import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";

import { streamEvents } from "@/lib/api/client";
import type { AssistantMessage, Citation } from "@/lib/api/types";
import { queryKeys } from "@/lib/query-keys";

import type { ConversationFeedbackKind } from "./ConversationFeedback";

export interface StreamFeedback {
  kind: ConversationFeedbackKind;
  message?: string;
  traceId?: string | null;
  code?: number;
  status?: number;
  retryAfter?: number | null;
}

interface ApiErrorLike {
  code?: unknown;
  status?: unknown;
  msg?: unknown;
  traceId?: unknown;
  retryAfter?: unknown;
}

function textValue(data: Record<string, unknown>, key: string): string | null {
  return typeof data[key] === "string" ? data[key] : null;
}

function citationValues(data: Record<string, unknown>): Citation[] {
  return Array.isArray(data.citations) ? (data.citations as Citation[]) : [];
}

/** 将 API 客户端异常转为可访问的用户反馈；未知异常不泄露内部细节。 */
export function feedbackFromError(error: unknown): StreamFeedback {
  const value = (typeof error === "object" && error !== null ? error : {}) as ApiErrorLike;
  return {
    kind: "failed",
    message: typeof value.msg === "string" ? value.msg : undefined,
    traceId: typeof value.traceId === "string" ? value.traceId : null,
    code: typeof value.code === "number" ? value.code : undefined,
    status: typeof value.status === "number" ? value.status : undefined,
    retryAfter: typeof value.retryAfter === "number" ? value.retryAfter : null,
  };
}

/**
 * 当前 SSE 草稿只存在于组件生命周期内；终态后一律失效服务端查询。
 * 不把流内容、任务状态或服务端错误码写入 Zustand 等全局 UI store。
 */
export function useMessageStream(conversationId: string) {
  const queryClient = useQueryClient();
  const controllerRef = useRef<AbortController | null>(null);
  const mountedRef = useRef(true);
  const previousConversationIdRef = useRef<string | null>(null);
  const [isStreaming, setIsStreaming] = useState(false);
  const [streamMessage, setStreamMessage] = useState<AssistantMessage | null>(null);
  const [streamCitations, setStreamCitations] = useState<Citation[]>([]);
  const [feedback, setFeedback] = useState<StreamFeedback | null>(null);

  useEffect(() => {
    const isConversationSwitch =
      previousConversationIdRef.current !== null &&
      previousConversationIdRef.current !== conversationId;
    previousConversationIdRef.current = conversationId;
    mountedRef.current = true;
    // 同一组件切换会话时，旧请求的 finally 不会接管新会话状态；在新生命周期主动复位。
    const resetTimer = isConversationSwitch
      ? window.setTimeout(() => {
          if (!mountedRef.current || controllerRef.current) return;
          setIsStreaming(false);
          setStreamMessage(null);
          setStreamCitations([]);
          setFeedback(null);
        }, 0)
      : null;
    return () => {
      if (resetTimer !== null) window.clearTimeout(resetTimer);
      mountedRef.current = false;
      const controller = controllerRef.current;
      controller?.abort();
      if (controllerRef.current === controller) controllerRef.current = null;
    };
  }, [conversationId]);

  const send = async (question: string) => {
    if (!question || controllerRef.current) return;

    const controller = new AbortController();
    controllerRef.current = controller;
    setIsStreaming(true);
    setFeedback(null);
    setStreamCitations([]);
    let activeMessageId: string | null = null;
    let receivedTerminal = false;
    const isCurrentRequest = () => mountedRef.current && controllerRef.current === controller;

    try {
      await streamEvents(`/conversations/${conversationId}/messages`, {
        body: { content: question },
        signal: controller.signal,
        onEvent: (event, envelope) => {
          if (!isCurrentRequest()) return;
          if (envelope.code !== 0) {
            receivedTerminal = true;
            setStreamMessage((current) =>
              current ? { ...current, status: "failed", finish_reason: "error" } : current
            );
            setFeedback({
              kind: "failed",
              message: envelope.msg,
              traceId: envelope.trace_id,
              code: envelope.code,
            });
            return;
          }

          if (event === "message_start") {
            activeMessageId = textValue(envelope.data, "message_id");
            if (!activeMessageId) return;
            setStreamMessage({
              id: activeMessageId,
              conversation_id: conversationId,
              role: "assistant",
              content: "",
              status: "streaming",
              rewritten_query: null,
              finish_reason: null,
              created_at: new Date().toISOString(),
            });
          } else if (event === "retrieval_done") {
            const citations = citationValues(envelope.data);
            setStreamCitations(citations);
            if (citations.length === 0) setFeedback({ kind: "no_evidence" });
          } else if (event === "delta") {
            const text = textValue(envelope.data, "text");
            if (text) {
              setStreamMessage((current) =>
                current ? { ...current, content: current.content + text } : current
              );
            }
          } else if (event === "message_end") {
            receivedTerminal = true;
            const finishReason = textValue(envelope.data, "finish_reason");
            setStreamMessage((current) =>
              current
                ? {
                    ...current,
                    status: "completed",
                    finish_reason: finishReason === "length" ? "length" : "stop",
                  }
                : current
            );
          } else if (event === "error") {
            receivedTerminal = true;
            setStreamMessage((current) =>
              current ? { ...current, status: "failed", finish_reason: "error" } : current
            );
            setFeedback({
              kind: "failed",
              message: envelope.msg,
              traceId: envelope.trace_id,
              code: envelope.code,
            });
          }
        },
      });
    } catch (error) {
      if (controller.signal.aborted && isCurrentRequest()) {
        setStreamMessage((current) =>
          current ? { ...current, status: "cancelled", finish_reason: "cancelled" } : current
        );
        setFeedback({ kind: "cancelled" });
      } else if (isCurrentRequest()) {
        setStreamMessage((current) =>
          current ? { ...current, status: "failed", finish_reason: "error" } : current
        );
        setFeedback(feedbackFromError(error));
      }
    } finally {
      if (activeMessageId) {
        void queryClient.invalidateQueries({ queryKey: queryKeys.messagesAll(conversationId) });
        void queryClient.invalidateQueries({
          queryKey: queryKeys.citationsAll(conversationId, activeMessageId),
        });
      }
      if (isCurrentRequest()) {
        if (!receivedTerminal && !controller.signal.aborted) {
          setStreamMessage((current) =>
            current ? { ...current, status: "failed", finish_reason: "error" } : current
          );
          setFeedback({ kind: "failed", message: "回答连接意外中断，请重试。" });
        }
        // 终态后立即让服务端消息查询接管，避免本地草稿与持久化记录重复展示。
        setStreamMessage(null);
        setStreamCitations([]);
        setIsStreaming(false);
        controllerRef.current = null;
      }
    }
  };

  return {
    feedback,
    isStreaming,
    send,
    streamCitations,
    streamMessage,
    cancel: () => controllerRef.current?.abort(),
  };
}
