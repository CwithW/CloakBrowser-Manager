import { useEffect, useRef, useState } from "react";
import { ArrowUp, Download, Folder, Upload, X } from "lucide-react";
import { api, type ProfileFileEntry } from "../lib/api";

export function ProfileFiles({ profileId, onClose }: { profileId: string; onClose: () => void }) {
  const [path, setPath] = useState("");
  const [entries, setEntries] = useState<ProfileFileEntry[]>([]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [refresh, setRefresh] = useState(0);
  const input = useRef<HTMLInputElement>(null);

  useEffect(() => {
    let cancelled = false;
    api.listFiles(profileId, path).then((result) => {
      if (!cancelled) setEntries(result.entries);
    }).catch((err) => {
      if (!cancelled) setError(err instanceof Error ? err.message : "Unable to list files");
    });
    return () => { cancelled = true; };
  }, [profileId, path, refresh]);

  const upload = async (files: FileList | null) => {
    if (!files) return;
    setBusy(true);
    setError("");
    try {
      for (const file of Array.from(files)) {
        if (file.size > 512 * 1024 * 1024) throw new Error("Upload exceeds the 512 MiB limit");
        await api.uploadFile(profileId, [path, file.name].filter(Boolean).join("/"), file);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Upload failed");
    } finally {
      setBusy(false);
      setRefresh((value) => value + 1);
      if (input.current) input.current.value = "";
    }
  };

  return (
    <div className="fixed inset-0 z-50 bg-black/60 flex items-center justify-center p-6">
      <div role="dialog" aria-label="Browser files" className="bg-surface-1 border border-border rounded-lg w-full max-w-2xl p-5">
        <div className="flex items-center justify-between mb-4">
          <h2 className="font-semibold">Browser files</h2>
          <button type="button" onClick={onClose} aria-label="Close file manager" className="btn-secondary"><X className="h-4 w-4" /></button>
        </div>
        <div className="flex items-center gap-3 mb-4">
          <button type="button" disabled={!path || busy} onClick={() => setPath(path.split("/").slice(0, -1).join("/"))} aria-label="Parent directory" className="btn-secondary"><ArrowUp className="h-4 w-4" /></button>
          <span className="text-sm flex-1 truncate">Home{path ? ` / ${path}` : ""}</span>
          <button type="button" disabled={busy} onClick={() => input.current?.click()} className="btn-secondary flex items-center gap-2"><Upload className="h-4 w-4" />{busy ? "Uploading..." : "Upload files"}</button>
          <input ref={input} type="file" multiple className="hidden" aria-label="Upload files to browser Home" onChange={(e) => void upload(e.target.files)} />
        </div>
        {error && <p role="alert" className="text-sm text-red-400 mb-3">{error}</p>}
        <div className="max-h-[60vh] overflow-y-auto space-y-1">
          {!entries.length && <p className="text-sm text-gray-500">No files</p>}
          {entries.map((entry) => (
            <div key={entry.path} className="flex items-center gap-3 p-2 rounded bg-surface-2">
              {entry.is_directory ? <button type="button" onClick={() => setPath(entry.path)} className="flex items-center gap-2 flex-1 text-left"><Folder className="h-4 w-4" />{entry.name}</button> : <>
                <span className="flex-1 truncate">{entry.name}</span>
                <span className="text-xs text-gray-500">{((entry.size ?? 0) / 1024).toFixed(1)} KiB</span>
                <a href={api.downloadFileUrl(profileId, entry.path)} download aria-label={`Download ${entry.name}`} className="btn-secondary"><Download className="h-4 w-4" /></a>
              </>}
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
