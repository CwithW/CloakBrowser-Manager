import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { api } from "../lib/api";
import { ProfileFiles } from "./ProfileFiles";

vi.mock("../lib/api", () => ({ api: {
  listFiles: vi.fn(), uploadFile: vi.fn(),
  downloadFileUrl: (id: string, path: string) => `/api/profiles/${id}/files/download?path=${path}`,
} }));

describe("ProfileFiles", () => {
  it("navigates folders, uploads to the chosen folder and exposes download links", async () => {
    vi.mocked(api.listFiles).mockResolvedValueOnce({ path: "", entries: [{ name: "Downloads", path: "Downloads", is_directory: true, size: null }] }).mockResolvedValue({ path: "Downloads", entries: [{ name: "test.txt", path: "Downloads/test.txt", is_directory: false, size: 4 }] });
    vi.mocked(api.uploadFile).mockResolvedValue({ name: "new.txt", size: 3 });
    render(<ProfileFiles profileId="one" onClose={vi.fn()} />);
    fireEvent.click(await screen.findByRole("button", { name: "Downloads" }));
    await waitFor(() => expect(api.listFiles).toHaveBeenCalledWith("one", "Downloads"));
    const file = new File(["new"], "new.txt");
    fireEvent.change(screen.getByLabelText("Upload files to browser Home"), { target: { files: [file] } });
    await waitFor(() => expect(api.uploadFile).toHaveBeenCalledWith("one", "Downloads/new.txt", file));
    expect(screen.getByLabelText("Download test.txt").getAttribute("href")).toContain("Downloads/test.txt");
  });
});
