export type MistakeBookPrintRequest = {
  action: 'print' | 'pdf'
  title: string
  html: string
}

export type MistakeBookPrintResult =
  | { status: 'submitted' | 'saved' | 'cancelled' | 'unconfirmed' }
  | { status: 'failed'; error: string }
