# helpers.R
# Local pilot patch for scienceverse/metacheck:sha-2d0eb12.
# The container endpoint calls read_paper(); the bundled helper refers to
# metacheck::read_grobid(), which is not exported by the installed package.

nz <- function(x) {
  if (is.null(x) || length(x) == 0) NULL else x
}

extract_uploaded_file <- function(mp) {
  if (is.null(mp$file)) {
    return(NULL)
  }

  if (is.list(mp$file) && !is.null(mp$file$datapath)) {
    return(mp$file$datapath)
  }

  if (is.list(mp$file) && length(mp$file) > 0) {
    return(sapply(mp$file, function(f) f$datapath))
  }

  NULL
}

error_response <- function(res, status, message) {
  res$status <- status
  res$serializer <- plumber::serializer_unboxed_json()
  list(error = message)
}

info_table <- function(paper, fields = c("title", "keywords", "doi", "description")) {
  cols <- unique(c(fields, "paper_id"))
  tryCatch(
    {
      metacheck::paper_table(paper, "info", cols)
    },
    error = function(e) {
      logger::log_warn("Could not build info table: {e$message}")
      paper$info
    }
  )
}

read_paper <- function(file_path, request_id) {
  logger::log_info("Reading paper: {request_id}")

  result <- tryCatch(
    {
      logger::log_info("Reading GROBID XML file")
      metacheck::read(file_path)
    },
    error = function(e) {
      logger::log_error("Error reading paper: {e$message}")
      e
    }
  )

  if (inherits(result, "error")) {
    return(list(success = FALSE, error = result$message))
  }

  logger::log_info("Paper read successfully: {request_id}")
  list(success = TRUE, paper = result)
}
