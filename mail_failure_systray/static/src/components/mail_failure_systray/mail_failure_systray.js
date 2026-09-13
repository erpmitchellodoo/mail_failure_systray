import { Component, onMounted, onWillUnmount, useState } from "@odoo/owl";
import { Dropdown } from "@web/core/dropdown/dropdown";
import { useDropdownState } from "@web/core/dropdown/dropdown_hooks";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { user } from "@web/core/user";
import { _t } from "@web/core/l10n/translation";
import { deserializeDateTime, formatDateTime } from "@web/core/l10n/dates";

export class MailFailureSystray extends Component {
    static template = "mail_failure_systray.Systray";
    static components = { Dropdown };
    static props = [];

    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        this.notification = useService("notification");
        this.dropdown = useDropdownState();
        this.state = useState({ category: "failed", rows: [], failed_count: 0, loading: false, busy: false, error: "" });
        this.tabs = [{ key: "failed", label: _t("Failed") }, { key: "pending", label: _t("Pending") }, { key: "recent", label: _t("Recent") }];
        this.alive = true;
        this.generation = 0;
        onMounted(() => {
            this.refresh(false);
            this.timer = setInterval(() => {
                if (!document.hidden && !this.state.busy && !this.state.loading) {
                    this.refresh(this.dropdown.isOpen);
                }
            }, 60000);
        });
        onWillUnmount(() => {
            this.alive = false;
            this.generation++;
            clearInterval(this.timer);
        });
    }

    async refresh(withRows = true) {
        const generation = ++this.generation;
        const category = this.state.category;
        this.state.loading = true;
        try {
            const [summary, rows] = await Promise.all([
                this.orm.call("mail.failure.systray.service", "get_systray_summary", []),
                withRows ? this.orm.call("mail.failure.systray.service", "get_systray_emails", [category, 10]) : Promise.resolve(null),
            ]);
            if (!this.alive || generation !== this.generation) {
                return;
            }
            this.state.failed_count = summary.failed_count;
            if (rows) {
                this.state.rows = rows;
            }
            this.state.error = "";
        } catch {
            if (this.alive && generation === this.generation) {
                this.state.rows = [];
                this.state.failed_count = 0;
                this.state.error = _t("Could not load your emails. Please refresh.");
            }
        } finally {
            if (this.alive && generation === this.generation) {
                this.state.loading = false;
            }
        }
    }

    beforeOpen() {
        this.state.category = "failed";
        return this.refresh();
    }

    selectCategory(category) {
        this.state.category = category;
        this.state.rows = [];
        return this.refresh();
    }

    date(value) {
        return value ? formatDateTime(deserializeDateTime(value)) : "";
    }

    age(value) {
        return value ? deserializeDateTime(value).toRelative() : "";
    }

    async process(row) {
        if (this.state.busy) {
            return;
        }
        this.state.busy = true;
        try {
            const result = await this.orm.call("mail.failure.systray.service", row.state === "exception" ? "retry_email" : "send_now", [row.mail_id]);
            this.notification.add(result.message, { type: result.type });
        } catch {
            this.notification.add(_t("The email could not be processed. Refresh and check its status before retrying."), { type: "warning" });
        } finally {
            if (this.alive) {
                this.state.busy = false;
                await this.refresh();
            }
        }
    }

    async openDocument(row) {
        try {
            const action = await this.orm.call("mail.failure.systray.service", "open_document", [row.id]);
            await this.action.doAction(action);
            this.dropdown.close();
        } catch {
            this.notification.add(_t("The source document is unavailable or access has changed."), { type: "warning" });
            await this.refresh();
        }
    }

    async openAll() {
        try {
            await this.action.doAction("mail_failure_systray.action_my_emails");
            this.dropdown.close();
        } catch {
            this.notification.add(_t("Could not open My Emails. Please try again."), { type: "warning" });
        }
    }
}

registry.category("systray").add("mail_failure_systray.Systray", {
    Component: MailFailureSystray,
    isDisplayed: () => user.isInternalUser,
}, { sequence: 19 });
