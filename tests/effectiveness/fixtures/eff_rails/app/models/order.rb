class Order < ApplicationRecord
  belongs_to :user
  belongs_to :product

  validates :total_cents, numericality: { only_integer: true }

  scope :recent, -> { order(created_at: :desc) }
end
