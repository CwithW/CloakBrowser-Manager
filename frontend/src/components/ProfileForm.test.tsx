import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import {
  DEFAULT_FINGERPRINT_STORAGE_QUOTA_ARG,
  KEEPASSXC_EXTENSION_ALLOW_ARG,
  KEEPASSXC_EXTENSION_ARG,
  ProfileForm,
} from "./ProfileForm";
import type { Profile } from "../lib/api";

describe("ProfileForm launch arguments", () => {
  it("enables the bundled KeePassXC-Browser extension for new profiles", () => {
    render(
      <ProfileForm
        profile={null}
        onSave={vi.fn()}
        onCancel={vi.fn()}
      />,
    );

    expect(screen.getByText(KEEPASSXC_EXTENSION_ALLOW_ARG)).not.toBeNull();
    expect(screen.getByText(KEEPASSXC_EXTENSION_ARG)).not.toBeNull();
    expect(screen.getByText(DEFAULT_FINGERPRINT_STORAGE_QUOTA_ARG)).not.toBeNull();
  });

  it("uses the requested behavior and screen defaults for new profiles", async () => {
    const onSave = vi.fn().mockResolvedValue(undefined);
    const { container } = render(
      <ProfileForm
        profile={null}
        onSave={onSave}
        onCancel={vi.fn()}
      />,
    );

    const humanize = screen.getByLabelText(
      "Human-like mouse, keyboard, and scroll behavior",
    ) as HTMLInputElement;
    const humanPreset = screen.getByText("Human Preset")
      .parentElement?.querySelector("select") as HTMLSelectElement;
    const screenResolution = screen.getByText("Screen Resolution")
      .parentElement?.querySelector("select") as HTMLSelectElement;

    expect(humanize.checked).toBe(true);
    expect(humanPreset.value).toBe("careful");
    expect(screenResolution.value).toBe("1280 × 720 (720p)");

    fireEvent.change(container.querySelector('input[placeholder="e.g. Amazon Seller #1"]')!, {
      target: { value: "Default profile" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Create" }));

    await waitFor(() => expect(onSave).toHaveBeenCalledOnce());
    expect(onSave).toHaveBeenCalledWith(expect.objectContaining({
      screen_width: 1280,
      screen_height: 720,
      humanize: true,
      human_preset: "careful",
      launch_args: expect.arrayContaining([
        DEFAULT_FINGERPRINT_STORAGE_QUOTA_ARG,
      ]),
    }));
  });

  it("allows the default KeePassXC-Browser argument to be removed", () => {
    render(
      <ProfileForm
        profile={null}
        onSave={vi.fn()}
        onCancel={vi.fn()}
      />,
    );

    const launchArg = screen.getByText(KEEPASSXC_EXTENSION_ARG);
    fireEvent.click(launchArg.querySelector("button")!);

    expect(screen.queryByText(KEEPASSXC_EXTENSION_ARG)).toBeNull();
  });
});

describe("ProfileForm browser types", () => {
  it.each(["chromium", "firefox"])("shows shared settings and native defaults for %s", async (type) => {
    const onSave = vi.fn().mockResolvedValue(undefined);
    render(<ProfileForm profile={null} onSave={onSave} onCancel={vi.fn()} />);
    fireEvent.change(screen.getByLabelText("Browser Type"), { target: { value: type } });
    expect(screen.queryByText("Fingerprint Seed")).toBeNull();
    expect(screen.queryByText("GPU Preset")).toBeNull();
    expect(screen.queryByText("Human Preset")).toBeNull();
    expect(screen.queryByLabelText("Headless browser")).toBeNull();
    expect((screen.getByLabelText("Enable KeePassXC-Browser") as HTMLInputElement).checked).toBe(true);
    expect((screen.getByLabelText("Use GPU when available") as HTMLInputElement).checked).toBe(true);
    for (const label of ["Proxy", "Timezone", "Locale", "Screen Resolution", "Launch Args", "Notes"]) {
      expect(screen.getByText(label)).not.toBeNull();
    }
    fireEvent.change(screen.getByPlaceholderText("e.g. Amazon Seller #1"), { target: { value: "Native" } });
    fireEvent.click(screen.getByRole("button", { name: "Create" }));
    await waitFor(() => expect(onSave).toHaveBeenCalledOnce());
    expect(onSave).toHaveBeenCalledWith(expect.objectContaining({ browser_type: type, platform: "linux", keepassxc_enabled: true, launch_args: [] }));
  });

  it("shows Camoufox fingerprint settings and validates JSON", async () => {
    const onSave = vi.fn().mockResolvedValue(undefined);
    render(<ProfileForm profile={null} onSave={onSave} onCancel={vi.fn()} />);
    fireEvent.change(screen.getByLabelText("Browser Type"), { target: { value: "camoufox" } });
    expect(screen.getByText("Fingerprint Seed")).not.toBeNull();
    fireEvent.change(screen.getByPlaceholderText("e.g. Amazon Seller #1"), { target: { value: "Camoufox" } });
    fireEvent.change(screen.getByLabelText("Camoufox Fingerprint (JSON)"), { target: { value: "[]" } });
    fireEvent.click(screen.getByRole("button", { name: "Create" }));
    expect(screen.getByRole("alert")).not.toBeNull();
    expect(onSave).not.toHaveBeenCalled();
    fireEvent.change(screen.getByLabelText("Camoufox Fingerprint (JSON)"), { target: { value: '{"navigator.hardwareConcurrency":8}' } });
    fireEvent.click(screen.getByRole("button", { name: "Create" }));
    await waitFor(() => expect(onSave).toHaveBeenCalledWith(expect.objectContaining({ camoufox_config: { "navigator.hardwareConcurrency": 8 } })));
  });

  it("locks type on edit and omits it from updates, preserving existing args", async () => {
    const onSave = vi.fn().mockResolvedValue(undefined);
    const profile = { id: "old", name: "Old", browser_type: "cloakbrowser", platform: "windows", launch_args: ["--existing"], tags: [] } as unknown as Profile;
    render(<ProfileForm profile={profile} onSave={onSave} onCancel={vi.fn()} />);
    expect((screen.getByLabelText("Browser Type") as HTMLSelectElement).disabled).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(onSave).toHaveBeenCalledOnce());
    expect(onSave.mock.calls[0][0]).not.toHaveProperty("browser_type");
    expect(onSave.mock.calls[0][0].launch_args).toEqual(["--existing"]);
  });
});
