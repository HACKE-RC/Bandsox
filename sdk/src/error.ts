export class BandSoxError extends Error {
  statusCode: number;

  constructor(message: string, statusCode: number) {
    super(message);
    this.name = "BandSoxError";
    this.statusCode = statusCode;
  }
}
