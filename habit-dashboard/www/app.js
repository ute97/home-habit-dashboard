(() => {
  "use strict";

  const labels = {
    today: "Today",
    habits: "Habit grid",
    tasks: "Tasks",
    goals: "Goals",
    insights: "Insights",
  };
  const weekdays = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
  const appBase = location.pathname.startsWith("/api/hassio_ingress/")
    ? `${location.pathname.replace(/\/$/, "")}/`
    : "/";
  const state = { view: "today", profileId: Number(localStorage.getItem("daymark-profile")) || 1, data: null, edit: null, vacationEditId: null };
  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
  let toastTimer;

  function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>"']/g, character => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    })[character]);
  }

  async function api(path, options = {}) {
    const response = await fetch(`${appBase}${path.replace(/^\//, "")}`, {
      ...options,
      headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    });
    const body = await response.json();
    if (!response.ok) throw new Error(body.error || `Request failed (${response.status})`);
    return body;
  }

  function showToast(message, error = false) {
    const toast = $("#toast");
    toast.textContent = message;
    toast.classList.toggle("error", error);
    toast.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => toast.classList.remove("show"), 2800);
  }

  function localDay(offset = 0) {
    const date = new Date();
    date.setDate(date.getDate() + offset);
    return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`;
  }

  function toDate(value) {
    const [year, month, day] = value.split("-").map(Number);
    return new Date(year, month - 1, day);
  }

  function formatDate(value, options = { month: "short", day: "numeric" }) {
    if (!value) return "No due date";
    return toDate(value).toLocaleDateString(undefined, options);
  }

  function scheduleText(habit) {
    const schedule = habit.schedule || {};
    switch (habit.schedule_type) {
      case "daily": return "Every day";
      case "weekdays": return (schedule.weekdays || []).map(day => weekdays[day]).join(", ");
      case "weekly_days": return `Weekly · ${(schedule.weekdays || []).map(day => weekdays[day]).join(", ")}`;
      case "weekly_target": return `${schedule.target}× per week`;
      case "monthly_dates": return `Monthly · ${(schedule.dates || []).map(day => `${day}${ordinal(day)}`).join(", ")}`;
      case "monthly_target": return `${schedule.target}× per month`;
      default: return "Habit";
    }
  }

  function ordinal(number) {
    const mod = number % 100;
    if (mod >= 11 && mod <= 13) return "th";
    return ({ 1: "st", 2: "nd", 3: "rd" })[number % 10] || "th";
  }

  function weekStart(value) {
    const date = toDate(value);
    const mondayOffset = (date.getDay() + 6) % 7;
    date.setDate(date.getDate() - mondayOffset);
    return localDateString(date);
  }

  function isoWeekKey(value) {
    const date = toDate(value);
    date.setDate(date.getDate() + 4 - (date.getDay() || 7));
    const yearStart = new Date(date.getFullYear(), 0, 1);
    const week = Math.ceil((((date - yearStart) / 86400000) + 1) / 7);
    return `${date.getFullYear()}-W${String(week).padStart(2, "0")}`;
  }

  function periodBounds(type, key) {
    if (type === "week") {
      const [year, week] = key.slice("week:".length).split("-W").map(Number);
      const januaryFourth = new Date(year, 0, 4);
      const start = new Date(januaryFourth);
      start.setDate(start.getDate() - ((start.getDay() + 6) % 7) + (week - 1) * 7);
      const end = new Date(start);
      end.setDate(end.getDate() + 6);
      return [localDateString(start), localDateString(end)];
    }
    const [year, month] = key.slice("month:".length).split("-").map(Number);
    return [`${year}-${String(month).padStart(2, "0")}-01`, localDateString(new Date(year, month, 0))];
  }

  function vacationCoversDay(day) {
    return (state.data?.vacations || []).some(range => range.start_date <= day && day <= range.end_date);
  }

  function vacationCoversPeriod(type, key) {
    const [start, end] = periodBounds(type, key);
    for (let day = start; day <= end; day = localDayFrom(day, 1)) {
      if (!vacationCoversDay(day)) return false;
    }
    return true;
  }

  function localDateString(date) {
    return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`;
  }

  function periodCompletions(habit, type, key) {
    return habit.completions.filter(day => type === "week"
      ? `week:${isoWeekKey(day)}` === key
      : `month:${day.slice(0, 7)}` === key).length;
  }

  function targetPeriodComplete(habit, type, key) {
    return periodCompletions(habit, type, key) >= Number(habit.schedule.target)
      || (habit.freeze_periods || []).includes(key)
      || vacationCoversPeriod(type, key);
  }

  function isScheduled(habit, day) {
    if (day < habit.created_on.slice(0, 10)) return false;
    if (vacationCoversDay(day)) return false;
    const date = toDate(day);
    const schedule = habit.schedule || {};
    switch (habit.schedule_type) {
      case "daily": return true;
      case "weekdays":
      case "weekly_days": return (schedule.weekdays || []).includes((date.getDay() + 6) % 7);
      case "monthly_dates": return (schedule.dates || []).includes(date.getDate());
      case "weekly_target": return !vacationCoversPeriod("week", `week:${isoWeekKey(day)}`)
        && periodCompletions(habit, "week", `week:${isoWeekKey(day)}`) < schedule.target;
      case "monthly_target": return !vacationCoversPeriod("month", `month:${day.slice(0, 7)}`)
        && periodCompletions(habit, "month", `month:${day.slice(0, 7)}`) < schedule.target;
      default: return false;
    }
  }

  function currentStreak(habit, today = state.data.today) {
    if (habit.schedule_type === "weekly_target") {
      let cursor = weekStart(today);
      if (!targetPeriodComplete(habit, "week", `week:${isoWeekKey(today)}`)) cursor = localDayFrom(cursor, -7);
      let count = 0;
      for (let period = 0; period < 53; period += 1) {
        if (!targetPeriodComplete(habit, "week", `week:${isoWeekKey(cursor)}`)) break;
        count += 1;
        cursor = localDayFrom(cursor, -7);
      }
      return { count, unit: "week" };
    }
    if (habit.schedule_type === "monthly_target") {
      let cursor = toDate(today);
      cursor.setDate(1);
      if (!targetPeriodComplete(habit, "month", `month:${today.slice(0, 7)}`)) cursor.setMonth(cursor.getMonth() - 1);
      let count = 0;
      for (let period = 0; period < 13; period += 1) {
        if (!targetPeriodComplete(habit, "month", `month:${localDateString(cursor).slice(0, 7)}`)) break;
        count += 1;
        cursor.setMonth(cursor.getMonth() - 1);
      }
      return { count, unit: "month" };
    }
    let streak = 0;
    for (let offset = 0; offset < 366; offset += 1) {
      const day = localDayFrom(today, -offset);
      const scheduled = isScheduled(habit, day);
      const complete = habit.completions.includes(day) || habit.freezes.includes(day);
      if (scheduled && !complete) {
        if (offset === 0) continue;
        break;
      }
      if (scheduled && complete) streak += 1;
    }
    return { count: streak, unit: ["daily", "weekdays"].includes(habit.schedule_type) ? "day" : "check-in" };
  }

  function localDayFrom(value, offset) {
    const date = toDate(value);
    date.setDate(date.getDate() + offset);
    return localDateString(date);
  }

  async function load() {
    try {
      state.data = await api(`/api/state?profile_id=${state.profileId}`);
      state.profileId = state.data.profile.id;
      localStorage.setItem("daymark-profile", String(state.profileId));
      render();
    } catch (error) {
      showToast(error.message, true);
    }
  }

  function render() {
    if (!state.data) return;
    const { profile, profiles, habits, tasks, goals, today } = state.data;
    $("#profileName").textContent = profile.name;
    $("#profileAvatar").textContent = profile.name.trim().charAt(0).toUpperCase() || "?";
    $("#freezeCount").textContent = `${profile.freeze_tokens} freeze token${profile.freeze_tokens === 1 ? "" : "s"}`;
    $("#profileSelect").innerHTML = profiles.map(item => `<option value="${item.id}" ${item.id === profile.id ? "selected" : ""}>${escapeHtml(item.name)}</option>`).join("");
    $("#crumb").textContent = labels[state.view];
    const now = toDate(today);
    $("#todayDate").textContent = now.toLocaleDateString(undefined, { weekday: "long", month: "long", day: "numeric", year: "numeric" }).toUpperCase();
    $("#datePill").textContent = now.toLocaleDateString(undefined, { month: "short", day: "numeric" });
    renderToday(habits, tasks, today);
    renderHabitGrid(habits, today);
    renderTasks(tasks, today);
    renderVacationList();
    renderGoals(goals);
    renderInsights(habits, today);
    $$(".view").forEach(view => view.classList.toggle("active", view.id === `view-${state.view}`));
    $$(".nav-item").forEach(item => item.classList.toggle("active", item.dataset.view === state.view));
  }

  function renderToday(habits, tasks, today) {
    const dueHabits = habits.filter(habit => habit.active && isScheduled(habit, today));
    const checked = dueHabits.filter(habit => habit.completions.includes(today)).length;
    const percentage = dueHabits.length ? Math.round(checked / dueHabits.length * 100) : 0;
    $("#todayPercent").textContent = `${percentage}%`;
    $("#todayRing").style.strokeDashoffset = String(314.16 * (1 - percentage / 100));
    $("#ringMessage").textContent = dueHabits.length === 0
      ? "Set a rhythm that feels right for you."
      : percentage === 100 ? "You showed up for yourself today. Let that be enough."
        : `${checked} of ${dueHabits.length} ${dueHabits.length === 1 ? "habit" : "habits"} checked in. Keep going at your own pace.`;
    $("#todayFooterCount").textContent = `${checked} of ${dueHabits.length} complete`;
    $("#emptyHabits").hidden = dueHabits.length > 0;
    $("#todayHabits").innerHTML = dueHabits.map(habit => {
      const done = habit.completions.includes(today);
      const streak = currentStreak(habit, today);
      return `<div class="habit-row">
        <button class="check-button ${done ? "checked" : ""}" data-toggle-habit="${habit.id}" data-day="${today}" aria-label="${done ? "Undo" : "Complete"} ${escapeHtml(habit.name)}">${done ? "✓" : ""}</button>
        <div class="habit-info ${done ? "complete" : ""}"><b>${escapeHtml(habit.name)}</b><small>${escapeHtml(scheduleText(habit))}</small></div>
        <div class="habit-row-actions">${streak.count ? `<span class="streak-label">✦ ${streak.count} ${streak.unit}${streak.count === 1 ? "" : "s"} streak</span>` : ""}<button class="mini-action" data-edit="habit" data-id="${habit.id}" aria-label="Edit ${escapeHtml(habit.name)}">···</button></div>
      </div>`;
    }).join("");
    const upcoming = tasks.filter(task => !task.done).sort((a, b) => (a.due_date || "9999").localeCompare(b.due_date || "9999")).slice(0, 3);
    $("#todayTasks").innerHTML = upcoming.length ? upcoming.map(task => `
      <div class="coming-row"><i class="coming-marker"></i><b>${escapeHtml(task.title)}${task.shared ? '<span class="task-scope">Shared</span>' : ""}</b><small>${task.due_date ? formatDate(task.due_date) : "Whenever you're ready"}</small></div>
    `).join("") : '<div class="coming-row"><b>No open tasks on your list.</b><small>Enjoy the space.</small></div>';
  }

  function renderHabitGrid(habits, today) {
    const active = habits.filter(habit => habit.active);
    const archived = habits.filter(habit => !habit.active);
    $("#emptyGrid").hidden = habits.length > 0;
    const days = Array.from({ length: 28 }, (_, index) => localDayFrom(today, index - 27));
    const header = `<div class="grid-row grid-days"><span class="grid-name">HABIT</span>${days.map(day => `<span class="day-label" title="${formatDate(day, { weekday: "long", month: "long", day: "numeric" })}">${toDate(day).getDate()}</span>`).join("")}</div>`;
    const row = habit => `
      <div class="grid-row">
        <button class="grid-name grid-habit-edit" data-edit="habit" data-id="${habit.id}" title="Edit ${escapeHtml(habit.name)}">${escapeHtml(habit.name)}</button>
        ${days.map(day => {
          const done = habit.completions.includes(day);
          const frozen = habit.freezes.includes(day);
          const future = day > today;
          const vacation = vacationCoversDay(day);
          const scheduled = isScheduled(habit, day);
          return `<button class="grid-cell ${done ? "complete" : frozen ? "frozen" : vacation ? "vacation" : scheduled ? "scheduled" : ""} ${future || !habit.active ? "future" : ""}" ${future || !scheduled || !habit.active ? "disabled" : `data-toggle-habit="${habit.id}" data-day="${day}"`} title="${formatDate(day, { weekday: "long", month: "short", day: "numeric" })}${done ? " · completed" : frozen ? " · streak protected" : vacation ? " · vacation" : scheduled ? " · tap to check in" : ""}" aria-label="${escapeHtml(habit.name)} ${day}${vacation ? " vacation" : scheduled ? done ? " completed" : " scheduled" : " not scheduled"}"></button>`;
        }).join("")}
      </div>`;
    $("#habitGrid").innerHTML = header + active.map(row).join("")
      + (archived.length ? `<div class="grid-row grid-days"><span class="grid-name">ARCHIVED</span>${days.map(() => '<span></span>').join("")}</div>${archived.map(habit => row(habit).replace('class="grid-row"', 'class="grid-row archived"')).join("")}` : "");
  }

  function renderTasks(tasks, today) {
    const open = tasks.filter(task => !task.done);
    $("#taskStats").innerHTML = `
      <div class="stat-box"><small>OPEN TASKS</small><b>${open.length}</b></div>
      <div class="stat-box"><small>DUE TODAY</small><b>${open.filter(task => task.due_date === today).length}</b></div>
      <div class="stat-box"><small>COMPLETED</small><b>${tasks.filter(task => task.done).length}</b></div>
    `;
    const filter = $("#taskFilter").value;
    const scopeFilter = $("#taskScopeFilter").value;
    const shown = tasks.filter(task => (filter === "all" || (filter === "done" ? task.done : !task.done))
      && (scopeFilter === "all" || Boolean(task.shared) === (scopeFilter === "shared")));
    $("#emptyTasks").hidden = shown.length > 0;
    $("#taskList").innerHTML = shown.map(task => `
      <div class="task-row ${task.done ? "completed" : ""}">
        <button class="check-button ${task.done ? "checked" : ""}" data-toggle-task="${task.id}" aria-label="${task.done ? "Mark open" : "Complete"} ${escapeHtml(task.title)}">${task.done ? "✓" : ""}</button>
        <div class="task-main"><b>${escapeHtml(task.title)}</b>${task.notes ? `<small>${escapeHtml(task.notes)}</small>` : ""}</div>
        ${task.shared ? '<span class="task-scope">Shared</span>' : ""}
        <span class="priority ${task.priority}">${task.priority}</span>
        <span class="due-label ${task.due_date && task.due_date < today && !task.done ? "overdue" : ""}">${task.due_date ? formatDate(task.due_date) : "No date"}</span>
        <button class="mini-action" data-edit="task" data-id="${task.id}" aria-label="Edit ${escapeHtml(task.title)}">···</button>
      </div>
    `).join("");
  }

  function renderGoals(goals) {
    const done = goals.filter(goal => Number(goal.progress) >= Number(goal.target)).length;
    $("#goalSummary").innerHTML = `
      <div class="stat-box"><small>ACTIVE GOALS</small><b>${goals.length - done}</b></div>
      <div class="stat-box"><small>COMPLETED</small><b>${done}</b></div>
      <div class="stat-box"><small>MILESTONES</small><b>${goals.reduce((sum, goal) => sum + goal.milestones.length, 0)}</b></div>
    `;
    $("#emptyGoals").hidden = goals.length > 0;
    $("#goalGrid").innerHTML = goals.map(goal => {
      const percent = Math.min(100, Math.round(Number(goal.progress) / Number(goal.target) * 100));
      return `<article class="goal-card">
        <div class="goal-card-top"><h2>${escapeHtml(goal.title)}</h2><span class="goal-pct">${percent}%</span></div>
        <p>${escapeHtml(goal.description || "Every step forward counts.")}</p>
        <div class="progress-track"><div class="progress-fill" style="width:${percent}%"></div></div>
        <div class="goal-progress-copy"><span>${escapeHtml(goal.progress)} / ${escapeHtml(goal.target)} ${escapeHtml(goal.unit)}</span><span>${goal.due_date ? `By ${formatDate(goal.due_date)}` : "No deadline"}</span></div>
        ${goal.milestones.length ? `<ul class="milestones">${goal.milestones.map(item => `<li>${escapeHtml(item)}</li>`).join("")}</ul>` : ""}
        <div class="goal-card-actions"><button class="mini-action" data-edit="goal" data-id="${goal.id}">Edit goal</button><button class="mini-action" data-delete="goal" data-id="${goal.id}">Delete</button></div>
      </article>`;
    }).join("");
  }

  function renderInsights(habits, today) {
    const active = habits.filter(habit => habit.active);
    const days = Array.from({ length: 28 }, (_, index) => localDayFrom(today, index - 27));
    let scheduledCount = 0;
    let completeCount = 0;
    active.forEach(habit => {
      if (habit.schedule_type === "weekly_target" || habit.schedule_type === "monthly_target") {
        const kind = habit.schedule_type === "weekly_target" ? "week" : "month";
        const target = Number(habit.schedule.target);
        const periods = new Set(days.filter(day => day <= today && day >= habit.created_on.slice(0, 10)).map(day => kind === "week"
          ? `week:${isoWeekKey(day)}` : `month:${day.slice(0, 7)}`));
        periods.forEach(key => {
          if (vacationCoversPeriod(kind, key)) return;
          const completed = Math.min(target, periodCompletions(habit, kind, key));
          scheduledCount += target;
          completeCount += completed + (targetPeriodComplete(habit, kind, key) ? target - completed : 0);
        });
      } else {
        days.forEach(day => {
          if (day <= today && day >= habit.created_on.slice(0, 10) && isScheduled(habit, day)) {
            scheduledCount += 1;
            if (habit.completions.includes(day) || habit.freezes.includes(day)) completeCount += 1;
          }
        });
      }
    });
    const current = active.filter(habit => currentStreak(habit, today).count > 0).length;
    const longest = active.reduce((best, habit) => Math.max(best, longestStreak(habit, days, today)), 0);
    const rate = scheduledCount ? Math.round(completeCount / scheduledCount * 100) : 0;
    $("#insightMetrics").innerHTML = `
      <div class="metric-box"><small>CHECK-IN RATE</small><b>${rate}%</b><em>last 28 days</em></div>
      <div class="metric-box"><small>CHECK-INS KEPT</small><b>${completeCount}</b><em>completed or protected</em></div>
      <div class="metric-box"><small>ACTIVE STREAKS</small><b>${current}</b><em>habits in a streak</em></div>
      <div class="metric-box"><small>BEST STREAK</small><b>${longest}</b><em>successful periods in range</em></div>
    `;
    const week = Array.from({ length: 7 }, (_, index) => localDayFrom(today, index - 6));
    const counts = week.map(day => active.filter(habit => habit.completions.includes(day)).length);
    const max = Math.max(1, ...counts);
    $("#weeklyChart").innerHTML = week.map((day, index) => `
      <div class="chart-day"><span class="chart-count">${counts[index]}</span><div class="chart-bar-area"><div class="chart-bar" style="height:${Math.max(2, counts[index] / max * 100)}%"></div></div><span class="chart-label">${toDate(day).toLocaleDateString(undefined, { weekday: "short" })}</span></div>
    `).join("");
    const rates = active.map(habit => {
      const scheduled = days.filter(day => day <= today && day >= habit.created_on.slice(0, 10) && isScheduled(habit, day));
      const completed = scheduled.filter(day => habit.completions.includes(day) || habit.freezes.includes(day));
      return { habit, rate: scheduled.length ? completed.length / scheduled.length : 0, scheduled };
    }).sort((a, b) => b.rate - a.rate);
    if (rates.length && rates[0].scheduled.length) {
      $("#strongestTitle").textContent = rates[0].rate >= .8 ? "A rhythm to be proud of" : "A rhythm is taking shape";
      $("#strongestCopy").textContent = `You've kept ${Math.round(rates[0].rate * 100)}% of scheduled check-ins this month.`;
      $("#bestHabit").textContent = `✦  ${rates[0].habit.name}`;
    } else {
      $("#strongestTitle").textContent = "Finding your flow";
      $("#strongestCopy").textContent = "A few more check-ins will reveal where you shine.";
      $("#bestHabit").textContent = "Your patterns are just beginning.";
    }
    $("#habitInsights").innerHTML = active.map(habit => {
      const scheduled = days.filter(day => day <= today && isScheduled(habit, day));
      const complete = scheduled.filter(day => habit.completions.includes(day) || habit.freezes.includes(day));
      let scheduledCount = scheduled.length;
      let completedCount = complete.length;
      if (habit.schedule_type === "weekly_target" || habit.schedule_type === "monthly_target") {
        const kind = habit.schedule_type === "weekly_target" ? "week" : "month";
        const target = Number(habit.schedule.target);
        const periods = new Set(days.filter(day => day <= today && day >= habit.created_on.slice(0, 10)).map(day => kind === "week"
          ? `week:${isoWeekKey(day)}` : `month:${day.slice(0, 7)}`));
        const scheduledPeriods = [...periods].filter(key => !vacationCoversPeriod(kind, key));
        scheduledCount = scheduledPeriods.length * target;
        completedCount = scheduledPeriods.reduce((sum, key) => {
          const done = Math.min(target, periodCompletions(habit, kind, key));
          return sum + done + (targetPeriodComplete(habit, kind, key) ? target - done : 0);
        }, 0);
      }
      const pct = scheduledCount ? Math.round(completedCount / scheduledCount * 100) : 0;
      const bars = Array.from({ length: 14 }, (_, index) => {
        const day = localDayFrom(today, index - 13);
        const isDone = habit.completions.includes(day) || habit.freezes.includes(day);
        return `<i style="height:${isDone ? 17 : isScheduled(habit, day) ? 5 : 2}px;opacity:${isDone ? 1 : .35}"></i>`;
      }).join("");
      const streak = currentStreak(habit, today);
      return `<div class="insight-habit"><span class="insight-habit-name">${escapeHtml(habit.name)}</span><span class="sparkline">${bars}</span><span class="insight-pct">${pct}%</span><span class="insight-streak">✦ ${streak.count} ${streak.unit}</span></div>`;
    }).join("") || '<div class="empty-state"><b>Your first insight is just around the corner.</b><p>Add a habit to start tracking your rhythm.</p></div>';
  }

  function longestStreak(habit, days, today) {
    if (habit.schedule_type === "weekly_target" || habit.schedule_type === "monthly_target") {
      const kind = habit.schedule_type === "weekly_target" ? "week" : "month";
      const periods = new Set(days.filter(day => day <= today && day >= habit.created_on.slice(0, 10)).map(day => kind === "week"
        ? `week:${isoWeekKey(day)}` : `month:${day.slice(0, 7)}`));
      const ordered = [...periods].sort();
      let best = 0;
      let streak = 0;
      ordered.forEach((key, index) => {
        const complete = targetPeriodComplete(habit, kind, key);
        if (index === ordered.length - 1 && !complete) streak = 0;
        else if (complete) {
          streak += 1;
          best = Math.max(best, streak);
        } else streak = 0;
      });
      return best;
    }
    let best = 0;
    let streak = 0;
    days.forEach(day => {
      if (day > today) return;
      const scheduled = isScheduled(habit, day);
      if (!scheduled) return;
      if (habit.completions.includes(day) || habit.freezes.includes(day)) {
        streak += 1;
        best = Math.max(best, streak);
      } else streak = 0;
    });
    return best;
  }

  function field(label, name, value = "", type = "text", options = {}) {
    const required = options.required ? "required" : "";
    const min = options.min !== undefined ? `min="${options.min}"` : "";
    const max = options.max !== undefined ? `max="${options.max}"` : "";
    const step = options.step ? `step="${options.step}"` : "";
    const placeholder = options.placeholder ? `placeholder="${escapeHtml(options.placeholder)}"` : "";
    const hint = options.hint ? `<small>${options.hint}</small>` : "";
    const content = type === "textarea"
      ? `<textarea name="${name}" ${required} ${placeholder}>${escapeHtml(value)}</textarea>`
      : `<input type="${type}" name="${name}" value="${escapeHtml(value)}" ${required} ${min} ${max} ${step} ${placeholder}>`;
    return `<div class="field"><label>${label}</label>${content}${hint}</div>`;
  }

  function openEditor(type, item = null) {
    state.edit = { type, id: item?.id ?? null };
    $("#dialogTitle").textContent = `${item ? "Edit" : "New"} ${type}`;
    $("#dialogEyebrow").textContent = item ? "MAKE A CHANGE" : "MAKE IT YOURS";
    const form = $("#dialogFields");
    if (type === "habit") {
      const kind = item?.schedule_type || "daily";
      const schedule = item?.schedule || {};
      form.innerHTML = `
        ${field("HABIT NAME", "name", item?.name || "", "text", { required: true, placeholder: "e.g. Morning stretch" })}
        ${field("A NOTE (OPTIONAL)", "description", item?.description || "", "textarea", { placeholder: "A little context or a reminder to yourself" })}
        <div class="field"><label>RHYTHM</label><select name="schedule_type">
          <option value="daily" ${kind === "daily" ? "selected" : ""}>Every day</option>
          <option value="weekdays" ${kind === "weekdays" ? "selected" : ""}>Selected weekdays</option>
          <option value="weekly_days" ${kind === "weekly_days" ? "selected" : ""}>Weekly on selected days</option>
          <option value="weekly_target" ${kind === "weekly_target" ? "selected" : ""}>A number of times per week</option>
          <option value="monthly_dates" ${kind === "monthly_dates" ? "selected" : ""}>On selected dates each month</option>
          <option value="monthly_target" ${kind === "monthly_target" ? "selected" : ""}>A number of times per month</option>
        </select></div>
        <div class="field schedule-weekdays"><label>WHICH DAYS?</label><div class="weekday-options">${weekdays.map((day, index) => `<label><input type="checkbox" name="weekdays" value="${index}" ${(schedule.weekdays || []).includes(index) ? "checked" : ""}>${day}</label>`).join("")}</div></div>
        ${field("TIMES PER WEEK", "weekly_target", schedule.target || 3, "number", { min: 1, max: 7, required: true })}
        ${field("DATES OF THE MONTH", "monthly_dates", (schedule.dates || [1]).join(", "), "text", { required: true, hint: "Use dates 1–31, separated by commas. A date that doesn't occur in a month is skipped." })}
        ${field("TIMES PER MONTH", "monthly_target", schedule.target || 4, "number", { min: 1, max: 31, required: true })}
      `;
      const toggleSchedule = () => {
        const selected = $('select[name="schedule_type"]', form).value;
        $(".schedule-weekdays", form).hidden = !["weekdays", "weekly_days"].includes(selected);
        $('input[name="weekly_target"]', form).closest(".field").hidden = selected !== "weekly_target";
        $('input[name="monthly_dates"]', form).closest(".field").hidden = selected !== "monthly_dates";
        $('input[name="monthly_target"]', form).closest(".field").hidden = selected !== "monthly_target";
        $('input[name="weekly_target"]', form).required = selected === "weekly_target";
        $('input[name="monthly_dates"]', form).required = selected === "monthly_dates";
        $('input[name="monthly_target"]', form).required = selected === "monthly_target";
        $$('input[name="weekdays"]', form).forEach(input => { input.required = false; });
      };
      $('select[name="schedule_type"]', form).addEventListener("change", toggleSchedule);
      toggleSchedule();
      if (item) {
        form.insertAdjacentHTML("beforeend", `<div class="field"><label><input type="checkbox" name="active" ${item.active ? "checked" : ""}> Habit is active</label></div>`);
      }
    } else if (type === "task") {
      form.innerHTML = `
        ${field("TASK", "title", item?.title || "", "text", { required: true, placeholder: "What needs doing?" })}
        ${field("NOTES (OPTIONAL)", "notes", item?.notes || "", "textarea")}
        <div class="form-row">${field("DUE DATE", "due_date", item?.due_date || "", "date")}<div class="field"><label>PRIORITY</label><select name="priority">${["low", "normal", "high"].map(option => `<option ${option === (item?.priority || "normal") ? "selected" : ""}>${option}</option>`).join("")}</select></div></div>
        <div class="field"><label><input type="checkbox" name="shared" ${item?.shared ? "checked" : ""}> Shared with all profiles</label><small>Anyone using this dashboard can see and complete a shared task.</small></div>
        ${item ? `<div class="field"><label><input type="checkbox" name="done" ${item.done ? "checked" : ""}> Mark as completed</label></div>` : ""}
      `;
    } else {
      form.innerHTML = `
        ${field("GOAL", "title", item?.title || "", "text", { required: true, placeholder: "What would you like to work toward?" })}
        ${field("THE WHY (OPTIONAL)", "description", item?.description || "", "textarea")}
        <div class="form-row">${field("PROGRESS", "progress", item?.progress ?? 0, "number", { min: 0, step: "any", required: true })}${field("TARGET", "target", item?.target ?? 1, "number", { min: 0.01, step: "any", required: true })}</div>
        <div class="form-row">${field("UNIT (OPTIONAL)", "unit", item?.unit || "", "text", { placeholder: "books, km, sessions" })}${field("TARGET DATE", "due_date", item?.due_date || "", "date")}</div>
        ${field("MILESTONES (OPTIONAL)", "milestones", (item?.milestones || []).join("\n"), "textarea", { hint: "One milestone per line." })}
      `;
    }
    $("#editor").showModal();
    const first = $('input:not([type="checkbox"]), select', form);
    if (first) first.focus();
  }

  function closeEditor() {
    $("#editor").close();
    state.edit = null;
  }

  async function saveEditor(event) {
    event.preventDefault();
    const { type, id } = state.edit;
    const form = new FormData(event.currentTarget);
    let path = `/api/${type === "habit" ? "habits" : type === "task" ? "tasks" : "goals"}`;
    let payload;
    if (type === "habit") {
      const scheduleType = form.get("schedule_type");
      let schedule = {};
      if (["weekdays", "weekly_days"].includes(scheduleType)) {
        schedule.weekdays = form.getAll("weekdays").map(Number);
        if (!schedule.weekdays.length) throw new Error("Choose at least one weekday.");
      } else if (scheduleType === "weekly_target") schedule.target = Number(form.get("weekly_target"));
      else if (scheduleType === "monthly_dates") {
        schedule.dates = [...new Set(String(form.get("monthly_dates")).split(",").map(value => Number(value.trim())).filter(Number.isInteger))];
        if (!schedule.dates.length) throw new Error("Enter one or more month dates separated by commas.");
      } else if (scheduleType === "monthly_target") schedule.target = Number(form.get("monthly_target"));
      payload = { profile_id: state.profileId, name: form.get("name"), description: form.get("description"), schedule_type: scheduleType, schedule };
      if (id) payload.active = form.has("active");
    } else if (type === "task") {
      payload = { profile_id: state.profileId, title: form.get("title"), notes: form.get("notes"), due_date: form.get("due_date") || null, priority: form.get("priority"), shared: form.has("shared") };
      if (id) payload.done = form.has("done");
    } else {
      payload = {
        profile_id: state.profileId, title: form.get("title"), description: form.get("description"),
        target: Number(form.get("target")), progress: Number(form.get("progress")), unit: form.get("unit"),
        due_date: form.get("due_date") || null,
        milestones: String(form.get("milestones") || "").split("\n").map(value => value.trim()).filter(Boolean),
      };
    }
    await api(id ? `${path}/${id}` : path, { method: id ? "PATCH" : "POST", body: JSON.stringify(payload) });
    closeEditor();
    showToast(`${type[0].toUpperCase()}${type.slice(1)} saved.`);
    await load();
  }

  async function toggleHabit(id, day) {
    await api(`/api/habits/${id}/toggle`, { method: "POST", body: JSON.stringify({ profile_id: state.profileId, date: day }) });
    await load();
  }

  async function toggleTask(id) {
    const task = state.data.tasks.find(item => item.id === id);
    await api(`/api/tasks/${id}`, { method: "PATCH", body: JSON.stringify({
      profile_id: state.profileId, title: task.title, notes: task.notes, due_date: task.due_date,
      priority: task.priority, done: !task.done, shared: task.shared,
    }) });
    await load();
  }

  async function deleteItem(type, id) {
    const item = type === "goal" ? state.data.goals.find(entry => entry.id === id) : state.data.tasks.find(entry => entry.id === id);
    const scope = type === "task" && item?.shared ? " This removes it from every profile." : "";
    if (!item || !window.confirm(`Delete "${item.title}"?${scope} This cannot be undone.`)) return;
    await api(`/api/${type === "goal" ? "goals" : "tasks"}/${id}`, {
      method: "DELETE", body: JSON.stringify({ profile_id: state.profileId }),
    });
    showToast(`${type[0].toUpperCase()}${type.slice(1)} deleted.`);
    await load();
  }

  async function addProfile() {
    const name = window.prompt("Name for this profile:");
    if (name === null) return;
    const result = await api("/api/profiles", { method: "POST", body: JSON.stringify({ name }) });
    state.profileId = result.id;
    await load();
  }

  async function renameProfile() {
    const name = window.prompt("Rename profile:", state.data.profile.name);
    if (name === null) return;
    await api(`/api/profiles/${state.profileId}`, { method: "PATCH", body: JSON.stringify({ name }) });
    await load();
  }

  async function resetProfile() {
    const name = state.data.profile.name;
    const confirmed = window.confirm(
      `Reset all data for ${name}? This clears habits and history, tasks, goals, vacations, and freeze tokens. Shared tasks created by this profile will be removed for everyone. The profile itself will remain. This cannot be undone. Export a backup first if needed.`,
    );
    if (!confirmed) return;
    await api(`/api/profiles/${state.profileId}/reset`, { method: "POST", body: JSON.stringify({}) });
    showToast(`${name}'s profile data was reset.`);
    await load();
  }

  async function removeProfile() {
    if (!window.confirm(`Remove ${state.data.profile.name} and all of its habits, tasks, vacations, goals, and history? Shared tasks created by this profile will also be removed. Export a backup first if needed.`)) return;
    await api(`/api/profiles/${state.profileId}`, { method: "DELETE", body: JSON.stringify({}) });
    state.profileId = state.data.profiles.find(profile => profile.id !== state.profileId).id;
    await load();
  }

  async function grantTokens() {
    const amount = Number(window.prompt("How many freeze tokens should be granted? (1–100)", "1"));
    if (!Number.isInteger(amount)) return;
    await api(`/api/profiles/${state.profileId}/tokens`, { method: "POST", body: JSON.stringify({ amount }) });
    showToast(`${amount} freeze token${amount === 1 ? "" : "s"} granted.`);
    await load();
  }

  function renderVacationList() {
    const vacations = state.data?.vacations || [];
    $("#vacationList").innerHTML = vacations.length ? vacations.map(range => {
      const editable = range.start_date >= state.data.today;
      return `<div class="vacation-row">
        <div><b>${formatDate(range.start_date, { month: "short", day: "numeric", year: "numeric" })} – ${formatDate(range.end_date, { month: "short", day: "numeric", year: "numeric" })}</b><small>${editable ? "Upcoming" : "Started · fixed"}</small></div>
        ${editable ? `<span class="vacation-actions"><button type="button" class="mini-action" data-edit-vacation="${range.id}" aria-label="Edit vacation">Edit</button><button type="button" class="mini-action" data-delete-vacation="${range.id}" aria-label="Delete vacation">Delete</button></span>` : ""}
      </div>`;
    }).join("") : '<p class="vacation-empty">No vacations planned.</p>';
  }

  function openVacationDialog(range = null) {
    state.vacationEditId = range?.id ?? null;
    $("#vacationDialogTitle").textContent = range ? "Edit vacation" : "Add vacation";
    $("#saveVacation").textContent = range ? "Save changes" : "Save vacation";
    $("#vacationStart").min = state.data.today;
    $("#vacationEnd").min = state.data.today;
    $("#vacationStart").value = range?.start_date || state.data.today;
    $("#vacationEnd").value = range?.end_date || state.data.today;
    $("#vacationEnd").min = $("#vacationStart").value;
    $("#profileMenu").hidden = true;
    $("#vacationDialog").showModal();
  }

  async function saveVacation(event) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const id = state.vacationEditId;
    const payload = {
      profile_id: state.profileId,
      start_date: form.get("start_date"),
      end_date: form.get("end_date"),
    };
    await api(id ? `/api/vacations/${id}` : `/api/profiles/${state.profileId}/vacations`, {
      method: id ? "PATCH" : "POST",
      body: JSON.stringify(payload),
    });
    $("#vacationDialog").close();
    state.vacationEditId = null;
    showToast("Vacation saved.");
    await load();
  }

  async function deleteVacation(id) {
    const range = state.data.vacations.find(item => item.id === id);
    if (!range || !window.confirm(`Delete the vacation from ${formatDate(range.start_date)} to ${formatDate(range.end_date)}?`)) return;
    await api(`/api/vacations/${id}`, {
      method: "DELETE",
      body: JSON.stringify({ profile_id: state.profileId }),
    });
    showToast("Vacation deleted.");
    await load();
  }

  async function exportData() {
    const data = await api("/api/export");
    const link = document.createElement("a");
    const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
    link.href = URL.createObjectURL(blob);
    link.download = `daymark-backup-${localDay()}.json`;
    link.click();
    URL.revokeObjectURL(link.href);
    showToast("Backup downloaded.");
  }

  async function importData(file) {
    const text = await file.text();
    if (!window.confirm("Importing replaces every profile and all tracker data on this Home Assistant. Export a backup first if you may need to undo this. Continue?")) return;
    let content;
    try { content = JSON.parse(text); } catch { throw new Error("That file is not valid JSON."); }
    await api("/api/import", { method: "POST", body: JSON.stringify(content) });
    state.profileId = Number(content.tables.profiles[0].id);
    showToast("Data imported.");
    await load();
  }

  function navigate(view) {
    if (!labels[view]) return;
    state.view = view;
    $("#profileMenu").hidden = true;
    render();
  }

  document.addEventListener("click", async event => {
    const target = event.target.closest("button");
    if (!target) return;
    try {
      if (target.id === "profileButton") {
        $("#profileMenu").hidden = !$("#profileMenu").hidden;
      } else if (target.dataset.view) navigate(target.dataset.view);
      else if (target.dataset.go) navigate(target.dataset.go);
      else if (target.dataset.add) openEditor(target.dataset.add);
      else if (target.id === "manageVacations") openVacationDialog();
      else if (target.dataset.editVacation) {
        const range = state.data.vacations.find(item => item.id === Number(target.dataset.editVacation));
        if (range) openVacationDialog(range);
      } else if (target.dataset.deleteVacation) await deleteVacation(Number(target.dataset.deleteVacation));
      else if (target.dataset.edit) {
        const list = target.dataset.edit === "habit" ? state.data.habits : target.dataset.edit === "task" ? state.data.tasks : state.data.goals;
        openEditor(target.dataset.edit, list.find(item => item.id === Number(target.dataset.id)));
      } else if (target.dataset.toggleHabit) await toggleHabit(Number(target.dataset.toggleHabit), target.dataset.day);
      else if (target.dataset.toggleTask) await toggleTask(Number(target.dataset.toggleTask));
      else if (target.dataset.delete) await deleteItem(target.dataset.delete, Number(target.dataset.id));
      else if (target.id === "quickAdd") openEditor("habit");
      else if (target.id === "addProfile") await addProfile();
      else if (target.id === "renameProfile") await renameProfile();
      else if (target.id === "resetProfile") await resetProfile();
      else if (target.id === "removeProfile") await removeProfile();
      else if (target.id === "grantTokens") await grantTokens();
      else if (target.id === "exportButton") await exportData();
      else if (target.id === "importButton") $("#importFile").click();
      else if (target.id === "closeDialog" || target.id === "cancelDialog") closeEditor();
      else if (target.id === "closeVacationDialog" || target.id === "cancelVacation") $("#vacationDialog").close();
    } catch (error) { showToast(error.message, true); }
  });

  $("#profileSelect").addEventListener("change", async event => {
    state.profileId = Number(event.target.value);
    await load();
  });
  $("#editorForm").addEventListener("submit", async event => {
    try { await saveEditor(event); } catch (error) { showToast(error.message, true); }
  });
  $("#taskFilter").addEventListener("change", () => state.data && renderTasks(state.data.tasks, state.data.today));
  $("#taskScopeFilter").addEventListener("change", () => state.data && renderTasks(state.data.tasks, state.data.today));
  $("#vacationForm").addEventListener("submit", async event => {
    try { await saveVacation(event); } catch (error) { showToast(error.message, true); }
  });
  $("#vacationStart").addEventListener("change", event => {
    $("#vacationEnd").min = event.target.value || state.data.today;
  });
  $("#importFile").addEventListener("change", async event => {
    const [file] = event.target.files;
    event.target.value = "";
    if (!file) return;
    try { await importData(file); } catch (error) { showToast(error.message, true); }
  });
  document.addEventListener("click", event => {
    if (!event.target.closest(".profile-wrap")) $("#profileMenu").hidden = true;
  });
  document.addEventListener("keydown", event => {
    const keyView = { "1": "today", "2": "habits", "3": "tasks", "4": "goals", "5": "insights" }[event.key];
    if (keyView && !["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement.tagName)) navigate(keyView);
    if (event.key === "Escape" && $("#profileMenu").hidden === false) $("#profileMenu").hidden = true;
  });
  load();
})();
