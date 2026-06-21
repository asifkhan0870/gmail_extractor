let currentJobId = null;

async function startExtraction() {

    const form = document.getElementById(
        "extractForm"
    );

    const formData = new FormData(
        form
    );

    const checked =
        formData.getAll("labels");

    if (checked.length === 0) {

        alert(
            "Please select at least one label."
        );

        return;
    }

    document.getElementById(
        "statusText"
    ).innerText = "Starting...";

    const response = await fetch(
        `/start-job/${USER_EMAIL}`,
        {
            method: "POST",
            body: formData
        }
    );

    const data =
        await response.json();

    currentJobId =
        data.job_id;

    pollProgress();
}

function pollProgress() {

    const interval = setInterval(
        async () => {

            const response =
                await fetch(
                    `/job-status/${currentJobId}`
                );

            const data =
                await response.json();

            const overallPercent =
                Math.min(
                    data.percent,
                    100
                );

            document.getElementById(
                "progressBar"
            ).style.width =
                overallPercent + "%";

            document.getElementById(
                "progressBar"
            ).innerText =
                overallPercent + "%";

            document.getElementById(
                "statusText"
            ).innerText =
                data.status;

            document.getElementById(
                "currentLabel"
            ).innerText =
                data.current_label || "-";

            document.getElementById(
                "processedCount"
            ).innerText =
                data.processed;

            document.getElementById(
                "emailsFound"
            ).innerText =
                data.emails_found;

            const labelTotal =
                data.label_total || 0;

            const labelProcessed =
                data.label_processed || 0;

            const labelPercent =
                labelTotal > 0
                    ? Math.min(
                        100,
                        Math.round(
                            (labelProcessed / labelTotal) * 100
                        )
                    )
                    : 0;

            document.getElementById(
                "labelBar"
            ).style.width =
                labelPercent + "%";

            document.getElementById(
                "labelCounter"
            ).innerText =
                `${labelProcessed} / ${labelTotal}`;

            if (
                data.status ===
                "completed"
            ) {

                clearInterval(
                    interval
                );

                const btn =
                    document.getElementById(
                        "downloadBtn"
                    );

                btn.href =
                    `/download/${currentJobId}`;

                btn.style.display =
                    "inline-block";

                document.getElementById(
                    "statusText"
                ).innerText =
                    "Completed";

                document.getElementById(
                    "labelBar"
                ).style.width = "100%";

                document.getElementById(
                    "labelCounter"
                ).innerText =
                    `${labelTotal} / ${labelTotal}`;
            }

        },
        1000
    );
}

function selectAllLabels() {

    document
        .querySelectorAll(
            'input[name="labels"]'
        )
        .forEach(
            x => x.checked = true
        );
}

function clearLabels() {

    document
        .querySelectorAll(
            'input[name="labels"]'
        )
        .forEach(
            x => x.checked = false
        );
}