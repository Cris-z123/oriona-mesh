"use client";

import { useParams } from "next/navigation";
import { useState } from "react";

import { RequireAuth } from "@/features/auth/RequireAuth";
import { DocumentList } from "@/features/documents/DocumentList";
import { UploadPanel } from "@/features/documents/UploadPanel";

export default function KnowledgeBaseDocumentsPage() {
  const params = useParams<{ knowledgeBaseId: string }>();
  const knowledgeBaseId = params.knowledgeBaseId;
  const [refreshKey, setRefreshKey] = useState(0);

  return (
    <RequireAuth>
      <main className="mx-auto max-w-4xl space-y-6 p-4">
        <h1 className="text-xl font-semibold">资料</h1>
        <UploadPanel
          knowledgeBaseId={knowledgeBaseId}
          onUploaded={() => setRefreshKey((k) => k + 1)}
        />
        <DocumentList key={refreshKey} knowledgeBaseId={knowledgeBaseId} />
      </main>
    </RequireAuth>
  );
}
